#!/usr/bin/env python3
"""Alfred Script Filter: find EC2 instances across profiles/regions, connect via SSM.

  ec2.py [filter words]

Configure Workflow settings: AWS_PROFILES, AWS_REGIONS (comma-separated, empty = from
~/.aws/config), EC2_CACHE_SECONDS (default 300).
"""
import configparser
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

# Alfred's PATH has none of these, so look in each install location; the first executable wins:
# the official installer, Homebrew on Apple silicon, pip/pipx user installs, then the system.
AWS_DIRS = ("/usr/local/bin", "/opt/homebrew/bin", "~/.local/bin", "/usr/bin")
CACHE_DIR = os.environ.get("alfred_workflow_cache", "/tmp/alfred-ec2-ssm")
CALL_TIMEOUT = 25  # seconds per aws call; the CLI's own socket timeouts are lower
CLI_TIMEOUTS = ["--cli-connect-timeout", "5", "--cli-read-timeout", "15"]


def find_aws():
    for d in AWS_DIRS:
        path = os.path.join(os.path.expanduser(d), "aws")
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


AWS = find_aws()  # once per run: Alfred starts the script fresh for every query
LOOKED_IN = "Looked in %s" % ", ".join(AWS_DIRS)


class AwsError(Exception):
    def __init__(self, kind, detail):
        Exception.__init__(self, detail)
        self.kind = kind
        self.detail = detail


# --- AWS calls -------------------------------------------------------------

def run_aws(args, timeout=CALL_TIMEOUT):
    """Run the aws CLI; return stdout or raise AwsError with a classified kind."""
    if not AWS:
        raise AwsError("missing", LOOKED_IN)
    env = dict(os.environ, AWS_PAGER="")
    try:
        p = subprocess.run([AWS] + args + ["--no-cli-pager"], capture_output=True, text=True,
                           timeout=timeout, env=env)
    except FileNotFoundError:
        raise AwsError("missing", "AWS CLI not found at %s" % AWS)
    except subprocess.TimeoutExpired:
        raise AwsError("timeout", "No answer after %d s" % timeout)
    if p.returncode != 0:
        raise AwsError(classify(p.stderr), error_line(p.stderr) or "aws exited %d" % p.returncode)
    return p.stdout


def error_line(text):
    """The CLI prints the actionable message last ("An error occurred (X) ...")."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    return lines[-1] if lines else ""


# Order matters: an SSO "token has expired" message also mentions credentials.
# "authfailure" is ambiguous (bad keys, or a region not enabled for the account);
# items_for() resolves it to "invalid" or "region".
ERROR_PATTERNS = [
    ("region", r"OptInRequired|not subscribed to this service"),
    ("authfailure", r"\(AuthFailure\)"),
    ("expired", r"ExpiredToken|RequestExpired|token has expired|session .*has expired|"
                r"Error loading SSO Token|Error when retrieving token from sso|refresh failed"),
    ("invalid", r"InvalidClientTokenId|SignatureDoesNotMatch|UnrecognizedClient|"
                r"Unable to locate credentials|could not be found|InvalidAccessKeyId|"
                r"security token included in the request is invalid"),
    ("denied", r"UnauthorizedOperation|AccessDenied|not authorized to perform"),
    # A misspelled region has no endpoint (DNS fails); it is not a slow AWS.
    ("endpoint", r"Could not connect to the endpoint URL"),
    ("timeout", r"Read timeout|Connect timeout|timed out"),
]


def classify(stderr):
    for kind, pattern in ERROR_PATTERNS:
        if re.search(pattern, stderr or "", re.I):
            return kind
    return "other"


def config_profiles():
    """Profile name -> section dict from the AWS config file (never the credentials file)."""
    path = os.environ.get("AWS_CONFIG_FILE") or os.path.expanduser("~/.aws/config")
    cp = configparser.RawConfigParser()
    try:
        cp.read(path)
    except configparser.Error:
        return {}
    out = {}
    for section in cp.sections():
        name = section[len("profile "):] if section.startswith("profile ") else section
        out[name.strip()] = dict(cp.items(section))
    return out


def is_sso(profile, profiles=None):
    section = (profiles if profiles is not None else config_profiles()).get(profile, {})
    return any(k.startswith("sso_") for k in section)


def split_var(name):
    return unique(v.strip() for v in os.environ.get(name, "").split(",") if v.strip())


def unique(values):
    """Drop repeats, keep order: a repeated target would fetch and write the same cache twice."""
    seen = []
    for v in values:
        if v not in seen:
            seen.append(v)
    return seen


def resolve_targets():
    """[(profile, region)] plus error items for profiles without a region (cached)."""
    path = cache_path("targets", os.environ.get("AWS_PROFILES", ""), os.environ.get("AWS_REGIONS", ""))
    cached = read_cache(path)
    if cached is not None:
        return unique(tuple(t) for t in cached["targets"]), cached["errors"]
    targets, errors = discover_targets()
    if targets:  # don't pin a broken setup for EC2_CACHE_SECONDS
        write_cache(path, {"targets": targets, "errors": errors})
    return targets, errors


def discover_targets():
    profiles = split_var("AWS_PROFILES") or unique(run_aws(["configure", "list-profiles"]).split())
    regions = split_var("AWS_REGIONS")
    if regions:
        return [(p, r) for p in profiles for r in regions], []

    def region_of(p):
        try:
            return run_aws(["configure", "get", "region", "--profile", p]).strip()
        except AwsError:
            return ""

    with ThreadPoolExecutor(max_workers=min(8, len(profiles) or 1)) as pool:
        found = list(pool.map(region_of, profiles))
    targets = [(p, r) for p, r in zip(profiles, found) if r]
    errors = [message("No region for profile %s" % p,
                      "Set region in ~/.aws/config or the Regions setting in Configure Workflow", icon_name="warning")
              for p, r in zip(profiles, found) if not r]
    return targets, errors


def parse_instances(data, profile, region):
    """Keep only the fields the workflow shows, from describe-instances JSON."""
    out = []
    for res in data.get("Reservations", []):
        for i in res.get("Instances", []):
            tags = {t.get("Key"): t.get("Value", "") for t in i.get("Tags") or []}
            out.append({
                "id": i.get("InstanceId", ""),
                "state": (i.get("State") or {}).get("Name", "unknown"),
                "type": i.get("InstanceType", ""),
                "private_ip": i.get("PrivateIpAddress", ""),
                "public_ip": i.get("PublicIpAddress", ""),
                "name": tags.get("Name", ""),
                "platform": i.get("PlatformDetails") or i.get("Platform") or "",
                "launch_time": i.get("LaunchTime", ""),
                "profile": profile,
                "region": region,
            })
    return out


def cache_path(kind, *parts):
    """Hash the exact values: "dev admin" and "dev_admin" are different profiles."""
    digest = hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:32]
    return os.path.join(CACHE_DIR, "%s-%s.json" % (kind, digest))


def cache_seconds():
    try:
        return max(0, int(os.environ.get("EC2_CACHE_SECONDS", "300")))
    except ValueError:
        return 300


def read_cache(path, now=None):
    now = time.time() if now is None else now
    try:
        if now - os.path.getmtime(path) < cache_seconds():
            with open(path) as f:
                return json.load(f)
    except (OSError, ValueError):
        pass  # missing, stale or half-written: refetch
    return None


def write_cache(path, data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    # Write then rename: Alfred kills the previous run on each keystroke.
    # mkstemp gives each write its own temp file, so parallel writers cannot collide.
    fd, tmp = tempfile.mkstemp(dir=CACHE_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def fetch(profile, region, now=None):
    """Instances for one profile+region, from cache when fresh. Errors are not cached."""
    path = cache_path("instances", profile, region)
    cached = read_cache(path, now)
    if cached is not None:
        return cached
    out = run_aws(["ec2", "describe-instances", "--profile", profile, "--region", region,
                   "--output", "json"] + CLI_TIMEOUTS)
    try:
        instances = parse_instances(json.loads(out or "{}"), profile, region)
    except ValueError:
        raise AwsError("other", "describe-instances returned non-JSON output")
    write_cache(path, instances)
    return instances


# --- Commands and URLs -----------------------------------------------------

def ssm_command(inst):
    return " ".join(shlex.quote(a) for a in [
        AWS, "ssm", "start-session", "--target", inst["id"],
        "--profile", inst["profile"], "--region", inst["region"]])


def sso_login_command(profile):
    return " ".join(shlex.quote(a) for a in [AWS, "sso", "login", "--profile", profile])


def console_url(region, instance_id):
    if region.startswith("cn-"):
        host = "console.amazonaws.cn"
    elif region.startswith("us-gov-"):
        host = "console.amazonaws-us-gov.com"
    else:
        host = "%s.console.aws.amazon.com" % region
    return "https://%s/ec2/home?region=%s#InstanceDetails:instanceId=%s" % (host, region, instance_id)


# --- Filtering, sorting, items -----------------------------------------------

def matches(inst, query):
    hay = " ".join([inst["name"], inst["id"], inst["private_ip"], inst["public_ip"], inst["type"]]).lower()
    return all(word in hay for word in query.lower().split())


def sort_key(inst):
    # Running first, then by name (unnamed last), then id for a stable order.
    return (inst["state"] != "running", inst["name"] == "", inst["name"].lower(), inst["id"])


# Instance state -> pre-rendered icon (make_icons.py). Unlisted states get "unknown".
STATE_ICONS = {
    "running": "running",
    "stopped": "stopped",
    "pending": "changing", "stopping": "changing", "rebooting": "changing",
    "shutting-down": "terminated", "terminated": "terminated",
}
# Problems with one region or a slow answer: other regions and a retry still work.
WARNING_KINDS = ("region", "endpoint", "timeout")


def icon(name):
    """Static icon from icons/ (see make_icons.py); Alfred resolves the path from the workflow folder."""
    return {"path": "icons/%s.png" % name}


def kind_icon(kind):
    return "warning" if kind in WARNING_KINDS else "error"


def message(title, subtitle="", arg=None, icon_name="error"):
    item = {"title": title, "subtitle": subtitle, "valid": arg is not None, "icon": icon(icon_name)}
    if arg is not None:
        item["arg"] = arg
    return item


def instance_item(inst):
    running = inst["state"] == "running"
    where = "%s/%s" % (inst["profile"], inst["region"])
    ip = inst["private_ip"] or "no private IP"
    url = console_url(inst["region"], inst["id"])
    detail = "\n".join([
        inst["name"] or "(no name)", inst["id"], "State: %s" % inst["state"], "Type: %s" % inst["type"],
        "Private IP: %s" % (inst["private_ip"] or "-"), "Public IP: %s" % (inst["public_ip"] or "-"),
        "Platform: %s" % (inst["platform"] or "-"), "Launched: %s" % (inst["launch_time"] or "-"), where])
    item = {
        "uid": "%s/%s" % (where, inst["id"]),
        "title": "%s · %s" % (inst["name"] or "(no name)", inst["state"]),
        "icon": icon(STATE_ICONS.get(inst["state"], "unknown")),
        "subtitle": " · ".join([inst["id"], inst["type"], ip, where]),
        "valid": running,
        "text": {"copy": inst["id"], "largetype": detail},
        "mods": {
            "cmd": ({"valid": True, "arg": inst["private_ip"], "subtitle": "Copy private IP %s" % inst["private_ip"]}
                    if inst["private_ip"] else {"valid": False, "subtitle": "No private IP"}),
            "alt": {"valid": True, "arg": url, "subtitle": "Open in AWS console (%s)" % inst["region"]},
        },
    }
    if running:
        item["arg"] = ssm_command(inst)
    else:
        item["subtitle"] += " · not running, no SSM"
    return item


ERROR_TITLES = {
    "expired": "Credentials expired",
    "invalid": "Credentials invalid or missing",
    "denied": "Access denied (ec2:DescribeInstances)",
    "timeout": "AWS did not answer in time",
    "region": "Region not enabled for this account",
    "endpoint": "Unknown region or unreachable endpoint",
    "authfailure": "AWS could not validate the credentials",
    "missing": "AWS CLI not found",
    "other": "AWS error",
}

ERROR_HINTS = {
    "region": "Enable the region (opt-in) in the account, or remove it from the Regions setting",
    "endpoint": "Check the region name in the Regions setting or ~/.aws/config, and the network",
}


def error_items(failures, profiles=None):
    """failures: [(profile, region, AwsError)] -> one item per profile+kind."""
    grouped = {}
    for profile, region, err in failures:
        grouped.setdefault((profile, err.kind), {"regions": [], "detail": err.detail})["regions"].append(region)
    items = []
    for (profile, kind), g in sorted(grouped.items()):
        title = "%s: profile %s (%s)" % (ERROR_TITLES[kind], profile, ", ".join(g["regions"]))
        if kind in ("expired", "invalid") and is_sso(profile, profiles):
            items.append(message(title, "↩ run aws sso login --profile %s in iTerm" % profile,
                                 sso_login_command(profile), icon_name="sso"))
        else:
            items.append(message(title, ERROR_HINTS.get(kind, g["detail"]), icon_name=kind_icon(kind)))
    return items


def build_items(query, instances, failures, profiles=None):
    errors = error_items(failures, profiles)
    hits = sorted((i for i in instances if matches(i, query)), key=sort_key)
    if hits:
        return errors + [instance_item(i) for i in hits]
    if instances:
        return errors + [message("No instances match '%s'" % query.strip(), "Matches name, id, IP or type",
                                   icon_name="empty")]
    if failures:
        return errors
    return [message("No EC2 instances found", "Searched the configured profiles and regions", icon_name="empty")]


def fetch_target(target):
    """One profile+region. Any failure becomes that target's error item, never a crash of the run."""
    try:
        return target, fetch(*target), None
    except AwsError as e:
        return target, [], e
    except Exception as e:
        return target, [], AwsError("other", "%s: %s" % (type(e).__name__, e))


def home_region(region):
    """A region that is always enabled in the same partition, for the credential check."""
    if region.startswith("cn-"):
        return "cn-north-1"
    if region.startswith("us-gov-"):
        return "us-gov-west-1"
    return "us-east-1"


def resolve_auth_failures(failures, instances):
    """AuthFailure means bad credentials or a region not enabled for the account.

    If the profile worked in another region, or a read-only sts get-caller-identity
    in an always-enabled region succeeds, the credentials are fine: the region is off.
    """
    worked = {i["profile"] for i in instances}
    checked = {}
    out = []
    for profile, region, err in failures:
        if err.kind == "authfailure":
            if profile in worked:
                err = AwsError("region", err.detail)
            else:
                key = (profile, home_region(region))
                if key not in checked:
                    try:
                        # stdout (account id, ARN) is discarded; nothing is shown or stored.
                        run_aws(["sts", "get-caller-identity", "--profile", profile,
                                 "--region", key[1], "--output", "json"] + CLI_TIMEOUTS)
                        checked[key] = None
                    except AwsError as e:
                        checked[key] = e if e.kind != "authfailure" else AwsError("invalid", e.detail)
                probe = checked[key]
                err = AwsError("region", err.detail) if probe is None else probe
        out.append((profile, region, err))
    return out


def items_for(query):
    if not AWS:
        return [message(ERROR_TITLES["missing"], "%s · install AWS CLI v2" % LOOKED_IN)]
    try:
        targets, region_errors = resolve_targets()
    except AwsError as e:
        return [message("%s: %s" % (ERROR_TITLES[e.kind], "listing profiles"), e.detail, icon_name=kind_icon(e.kind))]
    if not targets and not region_errors:
        return [message("No AWS profiles found", "Run aws configure, or set Profiles in Configure Workflow",
                        icon_name="hint")]

    instances, failures = [], []
    if targets:
        with ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
            for (profile, region), found, err in pool.map(fetch_target, targets):
                instances += found
                if err:
                    failures.append((profile, region, err))
    return region_errors + build_items(query, instances, resolve_auth_failures(failures, instances))


def main():
    query = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        items = items_for(query)
    except Exception as e:  # Alfred must always get valid JSON
        items = [message("ec2 workflow error: %s" % e, "Run ec2.py from a shell to see the traceback")]
    json.dump({"items": items, "skipknowledge": True}, sys.stdout)


if __name__ == "__main__":
    main()
