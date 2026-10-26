"""Run: /usr/bin/python3 -B -m unittest test_ec2  (from this folder)"""
import json
import os
import plistlib
import shlex
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

import ec2

HERE = os.path.dirname(os.path.abspath(__file__))


def raw_instance(iid, state="running", name=None, ip="10.0.0.1", itype="t3.micro", **extra):
    i = {"InstanceId": iid, "State": {"Name": state, "Code": 16}, "InstanceType": itype,
         "LaunchTime": "2026-10-01T10:00:00+00:00", "PlatformDetails": "Linux/UNIX"}
    if ip:
        i["PrivateIpAddress"] = ip
    if name is not None:
        i["Tags"] = [{"Key": "env", "Value": "prod"}, {"Key": "Name", "Value": name}]
    i.update(extra)
    return i


def describe(*instances):
    return {"Reservations": [{"Instances": list(instances)}]}


def inst(iid, state="running", name="", ip="10.0.0.1", itype="t3.micro", profile="default", region="us-east-2"):
    return {"id": iid, "state": state, "type": itype, "private_ip": ip, "public_ip": "", "name": name,
            "platform": "Linux/UNIX", "launch_time": "", "profile": profile, "region": region}


def completed(stdout="", stderr="", code=0):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class Parsing(unittest.TestCase):
    def test_keeps_the_fields_the_items_show(self):
        data = describe(raw_instance("i-1", name="web", PublicIpAddress="3.3.3.3"))
        (i,) = ec2.parse_instances(data, "default", "us-east-2")
        self.assertEqual(i, {"id": "i-1", "state": "running", "type": "t3.micro", "private_ip": "10.0.0.1",
                             "public_ip": "3.3.3.3", "name": "web", "platform": "Linux/UNIX",
                             "launch_time": "2026-10-01T10:00:00+00:00", "profile": "default",
                             "region": "us-east-2"})

    def test_terminated_instance_without_tags_or_ip_does_not_crash(self):
        # Terminated instances lose their IP and often have no Tags key at all.
        (i,) = ec2.parse_instances(describe(raw_instance("i-2", state="terminated", ip=None)), "p", "r")
        self.assertEqual((i["name"], i["private_ip"], i["state"]), ("", "", "terminated"))

    def test_flattens_all_reservations(self):
        data = {"Reservations": [{"Instances": [raw_instance("i-1")]}, {"Instances": [raw_instance("i-2")]}]}
        self.assertEqual([i["id"] for i in ec2.parse_instances(data, "p", "r")], ["i-1", "i-2"])

    def test_platform_falls_back_to_platform_field(self):
        raw = raw_instance("i-3")
        del raw["PlatformDetails"]
        raw["Platform"] = "windows"
        self.assertEqual(ec2.parse_instances(describe(raw), "p", "r")[0]["platform"], "windows")


class SortingAndFiltering(unittest.TestCase):
    def ids(self, query, instances):
        return [item["uid"].rsplit("/", 1)[1] for item in ec2.build_items(query, instances, [])]

    def test_running_first_then_by_name_unnamed_last(self):
        # You almost always want to connect, so connectable hosts lead the list.
        instances = [inst("i-a", "stopped", "alpha"), inst("i-z", "running", "zeta"),
                     inst("i-n", "running", ""), inst("i-b", "running", "Beta")]
        self.assertEqual(self.ids("", instances), ["i-b", "i-z", "i-n", "i-a"])

    def test_matches_name_id_ip_and_type(self):
        instances = [inst("i-0abc", name="web-1", ip="10.1.2.3", itype="m5.large"),
                     inst("i-0def", name="db-1", ip="10.9.9.9", itype="t3.micro")]
        for query, expected in (("web", ["i-0abc"]), ("0def", ["i-0def"]), ("10.1.2", ["i-0abc"]),
                                ("m5", ["i-0abc"]), ("WEB", ["i-0abc"])):
            self.assertEqual(self.ids(query, instances), expected, query)

    def test_all_words_must_match(self):
        instances = [inst("i-1", name="web-prod", itype="m5.large"), inst("i-2", name="web-dev", itype="t3.micro")]
        self.assertEqual(self.ids("web m5", instances), ["i-1"])

    def test_no_match_says_so_instead_of_empty_list(self):
        (item,) = ec2.build_items("nothing", [inst("i-1", name="web")], [])
        self.assertIn("No instances match 'nothing'", item["title"])
        self.assertFalse(item["valid"])


class Items(unittest.TestCase):
    def test_running_item_layout_and_actions(self):
        item = ec2.instance_item(inst("i-1", name="web", ip="10.0.0.5", profile="prod", region="eu-west-1"))
        self.assertEqual(item["title"], "web · running")
        self.assertEqual(item["subtitle"], "i-1 · t3.micro · 10.0.0.5 · prod/eu-west-1")
        self.assertTrue(item["valid"])
        self.assertEqual(item["arg"], ec2.ssm_command(inst("i-1", profile="prod", region="eu-west-1")))
        self.assertEqual(item["text"]["copy"], "i-1")  # ⌘C copies the instance id
        self.assertEqual(item["mods"]["cmd"]["arg"], "10.0.0.5")
        self.assertEqual(item["mods"]["alt"]["arg"], ec2.console_url("eu-west-1", "i-1"))

    def test_stopped_instance_offers_no_ssm_but_keeps_other_actions(self):
        # SSM to a stopped instance would only fail in a new iTerm window.
        item = ec2.instance_item(inst("i-1", state="stopped", name="web"))
        self.assertFalse(item["valid"])
        self.assertNotIn("arg", item)
        self.assertTrue(item["mods"]["cmd"]["valid"])
        self.assertTrue(item["mods"]["alt"]["valid"])
        self.assertIn("no SSM", item["subtitle"])

    def test_no_private_ip_disables_copy_ip(self):
        item = ec2.instance_item(inst("i-1", state="terminated", ip=""))
        self.assertFalse(item["mods"]["cmd"]["valid"])
        self.assertNotIn("arg", item["mods"]["cmd"])
        self.assertIn("no private IP", item["subtitle"])


class Commands(unittest.TestCase):
    def test_ssm_command(self):
        self.assertEqual(ec2.ssm_command(inst("i-0abc", profile="default", region="us-east-2")),
                         "/usr/local/bin/aws ssm start-session --target i-0abc --profile default --region us-east-2")

    def test_ssm_command_quotes_odd_profile_names(self):
        # The arg goes straight into a shell in iTerm: a profile name must not inject commands.
        cmd = ec2.ssm_command(inst("i-1", profile="my prof;rm -rf ~"))
        self.assertEqual(shlex.split(cmd)[-3], "my prof;rm -rf ~")

    def test_sso_login_command(self):
        self.assertEqual(ec2.sso_login_command("dev"), "/usr/local/bin/aws sso login --profile dev")

    def test_console_url_uses_the_instance_region(self):
        self.assertEqual(ec2.console_url("eu-west-1", "i-1"),
                         "https://eu-west-1.console.aws.amazon.com/ec2/home?region=eu-west-1"
                         "#InstanceDetails:instanceId=i-1")

    def test_console_url_other_partitions(self):
        self.assertTrue(ec2.console_url("cn-north-1", "i-1").startswith("https://console.amazonaws.cn/"))
        self.assertTrue(ec2.console_url("us-gov-west-1", "i-1").startswith("https://console.amazonaws-us-gov.com/"))


class ErrorClassification(unittest.TestCase):
    CASES = [
        ("Error when retrieving token from sso: Token has expired and refresh failed", "expired"),
        ("The SSO session associated with this profile has expired or is otherwise invalid.", "expired"),
        ("An error occurred (RequestExpired) when calling the DescribeInstances operation: Request has expired.",
         "expired"),
        # AuthFailure is ambiguous (bad keys or a disabled region); items_for resolves it.
        ("An error occurred (AuthFailure) when calling the DescribeInstances operation: "
         "AWS was not able to validate the provided access credentials", "authfailure"),
        ("An error occurred (InvalidClientTokenId) when calling the GetCallerIdentity operation", "invalid"),
        ("An error occurred (OptInRequired) when calling the DescribeInstances operation", "region"),
        ("Unable to locate credentials. You can configure credentials by running \"aws configure\".", "invalid"),
        ("The config profile (nope) could not be found", "invalid"),
        ("An error occurred (UnauthorizedOperation) when calling the DescribeInstances operation: "
         "You are not authorized to perform this operation.", "denied"),
        # A misspelled region has no endpoint; calling it a timeout sends you looking at the network.
        ("Could not connect to the endpoint URL: \"https://ec2.xx-fake-1.amazonaws.com/\"", "endpoint"),
        ("Read timeout on endpoint URL", "timeout"),
        ("something new and strange", "other"),
    ]

    def test_stderr_patterns(self):
        for stderr, kind in self.CASES:
            self.assertEqual(ec2.classify(stderr), kind, stderr)

    def test_run_aws_classifies_failures(self):
        with mock.patch("subprocess.run", return_value=completed(stderr="noise\nAn error occurred (AccessDenied) x", code=254)):
            with self.assertRaises(ec2.AwsError) as e:
                ec2.run_aws(["ec2", "describe-instances"])
        self.assertEqual(e.exception.kind, "denied")
        self.assertEqual(e.exception.detail, "An error occurred (AccessDenied) x")  # last line is the message

    def test_run_aws_timeout_and_missing_cli(self):
        for exc, kind in ((subprocess.TimeoutExpired("aws", 25), "timeout"), (FileNotFoundError(), "missing")):
            with mock.patch("subprocess.run", side_effect=exc):
                with self.assertRaises(ec2.AwsError) as e:
                    ec2.run_aws(["ec2", "describe-instances"])
            self.assertEqual(e.exception.kind, kind)

    def test_sso_profile_error_offers_login(self):
        profiles = {"dev": {"sso_session": "corp", "sso_account_id": "1"}, "default": {"region": "us-east-2"}}
        err = ec2.AwsError("expired", "Token has expired")
        (item,) = ec2.error_items([("dev", "us-east-1", err), ("dev", "eu-west-1", err)], profiles)
        self.assertTrue(item["valid"])
        self.assertEqual(item["arg"], "/usr/local/bin/aws sso login --profile dev")
        self.assertIn("us-east-1, eu-west-1", item["title"])  # one item per profile, not per region

    def test_iam_profile_error_is_informational(self):
        (item,) = ec2.error_items([("default", "us-east-2", ec2.AwsError("invalid", "AuthFailure"))],
                                  {"default": {"region": "us-east-2"}})
        self.assertFalse(item["valid"])
        self.assertNotIn("arg", item)

    def test_access_denied_on_sso_profile_does_not_offer_login(self):
        # Logging in again will not fix a missing IAM permission.
        (item,) = ec2.error_items([("dev", "r", ec2.AwsError("denied", "x"))], {"dev": {"sso_start_url": "u"}})
        self.assertFalse(item["valid"])

    def test_errors_show_above_instances_from_working_regions(self):
        items = ec2.build_items("", [inst("i-1", name="web")],
                                [("p", "eu-west-1", ec2.AwsError("timeout", "slow"))], {})
        self.assertIn("did not answer", items[0]["title"])
        self.assertEqual(items[1]["title"], "web · running")

    def test_sso_detection_reads_profile_sections(self):
        with tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False) as f:
            f.write("[default]\nregion = us-east-2\n\n[profile dev]\nsso_session = corp\nregion = eu-west-1\n")
        try:
            with mock.patch.dict(os.environ, {"AWS_CONFIG_FILE": f.name}):
                self.assertTrue(ec2.is_sso("dev"))
                self.assertFalse(ec2.is_sso("default"))
                self.assertFalse(ec2.is_sso("unknown"))
        finally:
            os.unlink(f.name)


class CachingAndTargets(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(ec2, "CACHE_DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_describe_call_is_read_only_and_scoped(self):
        out = json.dumps(describe(raw_instance("i-1", name="web")))
        with mock.patch("subprocess.run", return_value=completed(out)) as run:
            ec2.fetch("prod", "eu-west-1")
        args = run.call_args[0][0]
        self.assertEqual(args[:3], ["/usr/local/bin/aws", "ec2", "describe-instances"])
        self.assertEqual(args[args.index("--profile") + 1], "prod")
        self.assertEqual(args[args.index("--region") + 1], "eu-west-1")
        self.assertEqual(args[args.index("--output") + 1], "json")

    def test_second_call_within_ttl_uses_cache(self):
        out = json.dumps(describe(raw_instance("i-1")))
        with mock.patch("subprocess.run", return_value=completed(out)) as run:
            first = ec2.fetch("p", "r")
            second = ec2.fetch("p", "r")
        self.assertEqual(run.call_count, 1)  # typing must not hit AWS per keystroke
        self.assertEqual(first, second)

    def test_stale_cache_refetches(self):
        out = json.dumps(describe(raw_instance("i-1")))
        with mock.patch.dict(os.environ, {"EC2_CACHE_SECONDS": "300"}), \
                mock.patch("subprocess.run", return_value=completed(out)) as run:
            ec2.fetch("p", "r")
            ec2.fetch("p", "r", now=time.time() + 301)
        self.assertEqual(run.call_count, 2)

    def test_errors_are_not_cached(self):
        # After `aws sso login` the next keystroke must retry, not replay the error.
        good = completed(json.dumps(describe(raw_instance("i-1"))))
        with mock.patch("subprocess.run", side_effect=[completed(stderr="ExpiredToken", code=255), good]):
            with self.assertRaises(ec2.AwsError):
                ec2.fetch("p", "r")
            self.assertEqual(len(ec2.fetch("p", "r")), 1)

    def test_targets_from_variables_skip_discovery(self):
        env = {"AWS_PROFILES": "a, b", "AWS_REGIONS": "us-east-1,eu-west-1"}
        with mock.patch.dict(os.environ, env), mock.patch("subprocess.run") as run:
            targets, errors = ec2.resolve_targets()
        run.assert_not_called()
        self.assertEqual(targets, [("a", "us-east-1"), ("a", "eu-west-1"), ("b", "us-east-1"), ("b", "eu-west-1")])
        self.assertEqual(errors, [])

    def test_default_targets_from_aws_config(self):
        def fake(args, **kw):
            if args[1:3] == ["configure", "list-profiles"]:
                return completed("default\nnoregion\n")
            profile = args[args.index("--profile") + 1]
            return completed("us-east-2\n") if profile == "default" else completed(code=1)
        env = {"AWS_PROFILES": "", "AWS_REGIONS": ""}
        with mock.patch.dict(os.environ, env), mock.patch("subprocess.run", side_effect=fake):
            targets, errors = ec2.resolve_targets()
        self.assertEqual(targets, [("default", "us-east-2")])
        self.assertIn("No region for profile noregion", errors[0]["title"])


def describe_ok(*ids):
    return completed(json.dumps(describe(*[raw_instance(i) for i in ids])))


class ReviewRegressions(unittest.TestCase):
    """Defects found in review: each must stay fixed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(ec2, "CACHE_DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        access = mock.patch("os.access", return_value=True)
        access.start()
        self.addCleanup(access.stop)

    def describe_calls(self, run):
        return [c[0][0] for c in run.call_args_list if c[0][0][1:3] == ["ec2", "describe-instances"]]

    # 1. Duplicate targets used to make two threads race on one temp file and crash the run.
    def test_repeated_regions_and_profiles_are_fetched_once(self):
        env = {"AWS_PROFILES": "default,default", "AWS_REGIONS": "us-east-2, us-east-2"}
        with mock.patch.dict(os.environ, env), mock.patch("subprocess.run", return_value=describe_ok("i-1")) as run:
            items = ec2.items_for("")
        self.assertEqual(len(self.describe_calls(run)), 1)
        self.assertEqual([i["title"] for i in items], ["(no name) · running"])  # one item, not two

    def test_parallel_writes_to_one_cache_file_do_not_collide(self):
        path = ec2.cache_path("instances", "p", "r")
        errors = []

        def write(n):
            try:
                ec2.write_cache(path, [n])
            except Exception as e:  # pragma: no cover - the failure being guarded against
                errors.append(e)
        threads = [threading.Thread(target=write, args=(n,)) for n in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual([f for f in os.listdir(self.tmp.name) if f.endswith(".tmp")], [])  # no litter

    def test_unexpected_error_in_one_target_keeps_the_others(self):
        def fake_fetch(profile, region):
            if region == "eu-west-1":
                raise OSError(28, "No space left on device")
            return [inst("i-ok", name="web", region=region)]
        env = {"AWS_PROFILES": "default", "AWS_REGIONS": "us-east-2,eu-west-1"}
        with mock.patch.dict(os.environ, env), mock.patch.object(ec2, "fetch", side_effect=fake_fetch):
            items = ec2.items_for("")
        titles = [i["title"] for i in items]
        self.assertIn("web · running", titles)
        self.assertTrue(any("AWS error: profile default (eu-west-1)" in t for t in titles), titles)
        self.assertFalse(any("ec2 workflow error" in t for t in titles))

    # 2. A disabled opt-in region returns AuthFailure; it must not be blamed on credentials.
    AUTH = completed(stderr="An error occurred (AuthFailure) when calling the DescribeInstances operation: "
                            "AWS was not able to validate the provided access credentials", code=254)

    def fake_aws(self, sts):
        def run(args, **kw):
            if args[1:3] == ["sts", "get-caller-identity"]:
                return sts
            region = args[args.index("--region") + 1]
            return self.AUTH if region == "af-south-1" else describe_ok("i-1")
        return run

    def test_auth_failure_in_one_region_while_another_works_is_region_not_enabled(self):
        env = {"AWS_PROFILES": "dev", "AWS_REGIONS": "us-east-2,af-south-1"}
        with mock.patch.dict(os.environ, env), mock.patch.object(ec2, "config_profiles", return_value={"dev": {"sso_session": "x"}}), \
                mock.patch("subprocess.run", side_effect=self.fake_aws(completed("{}"))) as run:
            items = ec2.items_for("")
        self.assertEqual(items[0]["title"], "Region not enabled for this account: profile dev (af-south-1)")
        self.assertFalse(items[0]["valid"])  # no pointless sso login
        self.assertNotIn("sts", [c[0][0][1] for c in run.call_args_list])  # no extra call needed

    def test_only_region_failing_is_checked_with_read_only_sts(self):
        env = {"AWS_PROFILES": "dev", "AWS_REGIONS": "af-south-1"}
        with mock.patch.dict(os.environ, env), mock.patch.object(ec2, "config_profiles", return_value={"dev": {"sso_session": "x"}}), \
                mock.patch("subprocess.run", side_effect=self.fake_aws(completed('{"Account": "1"}'))) as run:
            items = ec2.items_for("")
        self.assertIn("Region not enabled", items[0]["title"])
        self.assertNotIn("1", items[0]["subtitle"])  # sts output is never shown
        sts = [c[0][0] for c in run.call_args_list if c[0][0][1] == "sts"]
        self.assertEqual(len(sts), 1)
        self.assertEqual(sts[0][1:3], ["sts", "get-caller-identity"])
        self.assertEqual(sts[0][sts[0].index("--region") + 1], "us-east-1")

    def test_auth_failure_with_bad_credentials_stays_a_credential_error(self):
        bad = completed(stderr="An error occurred (InvalidClientTokenId) when calling the GetCallerIdentity operation", code=254)
        env = {"AWS_PROFILES": "dev", "AWS_REGIONS": "af-south-1"}
        with mock.patch.dict(os.environ, env), mock.patch.object(ec2, "config_profiles", return_value={"dev": {"sso_session": "x"}}), \
                mock.patch("subprocess.run", side_effect=self.fake_aws(bad)):
            (item,) = ec2.items_for("")
        self.assertIn("Credentials invalid", item["title"])
        self.assertEqual(item["arg"], "/usr/local/bin/aws sso login --profile dev")

    # 3. A misspelled region is not a timeout.
    def test_unknown_region_names_the_region(self):
        bad = completed(stderr='Could not connect to the endpoint URL: "https://ec2.xx-fake-1.amazonaws.com/"', code=255)
        env = {"AWS_PROFILES": "default", "AWS_REGIONS": "xx-fake-1"}
        with mock.patch.dict(os.environ, env), mock.patch("subprocess.run", return_value=bad):
            (item,) = ec2.items_for("")
        self.assertEqual(item["title"], "Unknown region or unreachable endpoint: profile default (xx-fake-1)")
        self.assertFalse(item["valid"])

    # 4. Profiles that differ only in characters a sanitizer would replace must not share a cache.
    def test_similar_profile_names_get_separate_caches(self):
        self.assertNotEqual(ec2.cache_path("instances", "dev admin", "r"), ec2.cache_path("instances", "dev_admin", "r"))
        with mock.patch("subprocess.run", side_effect=[describe_ok("i-space"), describe_ok("i-underscore")]):
            a = ec2.fetch("dev admin", "us-east-1")
            b = ec2.fetch("dev_admin", "us-east-1")
        self.assertEqual((a[0]["id"], a[0]["profile"]), ("i-space", "dev admin"))
        self.assertEqual((b[0]["id"], b[0]["profile"]), ("i-underscore", "dev_admin"))


class AlwaysValidJson(unittest.TestCase):
    def run_main(self, query):
        with mock.patch("sys.argv", ["ec2.py", query]), mock.patch("sys.stdout") as out:
            ec2.main()
        return json.loads("".join(c[0][0] for c in out.write.call_args_list))

    def test_cli_missing(self):
        with mock.patch("os.access", return_value=False):
            items = self.run_main("web")["items"]
        self.assertEqual(items[0]["title"], "AWS CLI not found")

    def test_unexpected_crash_still_prints_json(self):
        with mock.patch.object(ec2, "items_for", side_effect=KeyError("boom")):
            items = self.run_main("")["items"]
        self.assertIn("ec2 workflow error", items[0]["title"])


class Icons(unittest.TestCase):
    """The icon says at a glance whether ↩ can connect (running), the host is down, mid-change
    or gone; problems that need a login, a config fix or just a retry must look different."""

    def path(self, item):
        return item["icon"]["path"]

    def test_each_instance_state_gets_its_icon(self):
        want = {"running": "running", "stopped": "stopped",
                "pending": "changing", "stopping": "changing", "rebooting": "changing",
                "shutting-down": "terminated", "terminated": "terminated",
                "something-new": "unknown"}
        for state, name in want.items():
            self.assertEqual(self.path(ec2.instance_item(inst("i-1", state=state))), "icons/%s.png" % name, state)

    def test_sso_login_item_gets_the_key_icon(self):
        profiles = {"dev": {"sso_session": "corp"}}
        for kind in ("expired", "invalid"):
            (item,) = ec2.error_items([("dev", "r", ec2.AwsError(kind, "x"))], profiles)
            self.assertEqual(self.path(item), "icons/sso.png", kind)

    def test_region_problems_and_timeouts_are_warnings_other_errors_are_errors(self):
        # A disabled or misspelled region (or a slow answer) leaves other regions usable.
        for kind, name in (("region", "warning"), ("endpoint", "warning"), ("timeout", "warning"),
                           ("expired", "error"), ("invalid", "error"), ("denied", "error"),
                           ("other", "error")):
            (item,) = ec2.error_items([("iam", "r", ec2.AwsError(kind, "x"))], {"iam": {"region": "r"}})
            self.assertEqual(self.path(item), "icons/%s.png" % name, kind)

    def test_empty_results_use_the_search_icon(self):
        self.assertEqual(self.path(ec2.build_items("zzz", [inst("i-1")], [])[0]), "icons/empty.png")
        self.assertEqual(self.path(ec2.build_items("", [], [])[0]), "icons/empty.png")

    def test_setup_problems(self):
        with mock.patch.object(ec2.os, "access", return_value=False):
            self.assertEqual(self.path(ec2.items_for("")[0]), "icons/error.png")
        with mock.patch.object(ec2.os, "access", return_value=True), \
                mock.patch.object(ec2, "resolve_targets", return_value=([], [])):
            self.assertEqual(self.path(ec2.items_for("")[0]), "icons/hint.png")  # no profiles: how to set up
        with mock.patch.object(ec2.os, "access", return_value=True), \
                mock.patch.object(ec2, "resolve_targets", side_effect=ec2.AwsError("timeout", "slow")):
            self.assertEqual(self.path(ec2.items_for("")[0]), "icons/warning.png")
        with mock.patch.object(ec2, "run_aws", side_effect=lambda a: "" if a[1] == "get" else "p1\n"), \
                mock.patch.dict(os.environ, {"AWS_REGIONS": "", "AWS_PROFILES": ""}):
            (_, errors) = ec2.discover_targets()
        self.assertEqual(self.path(errors[0]), "icons/warning.png")  # profile without a region

    def test_crash_item_has_error_icon(self):
        with mock.patch("sys.argv", ["ec2.py", ""]), mock.patch("sys.stdout") as out, \
                mock.patch.object(ec2, "items_for", side_effect=KeyError("boom")):
            ec2.main()
        data = json.loads("".join(c[0][0] for c in out.write.call_args_list))
        self.assertEqual(self.path(data["items"][0]), "icons/error.png")

    def test_every_icon_the_script_names_is_rendered_by_make_icons_and_exists(self):
        # A typo or a forgotten make_icons run shows Alfred's blank default icon.
        import make_icons
        names = set(ec2.STATE_ICONS.values()) | {"unknown", "sso", "warning", "error", "hint", "empty"}
        for name in names:
            self.assertIn(name, make_icons.JOBS)
            self.assertTrue(os.path.isfile(os.path.join(HERE, "icons", name + ".png")), name)


class PlistWiring(unittest.TestCase):
    """The plist must route every modifier the script emits to the right kind of action."""

    MOD_FLAGS = {"cmd": 1048576, "alt": 524288, "ctrl": 262144, "shift": 131072}

    def setUp(self):
        with open(os.path.join(HERE, "info.plist"), "rb") as f:
            self.plist = plistlib.load(f)
        self.objects = {o["uid"]: o for o in self.plist["objects"]}
        (self.filter,) = [o for o in self.plist["objects"] if o["type"] == "alfred.workflow.input.scriptfilter"]
        self.routes = {c["modifiers"]: self.objects[c["destinationuid"]]["type"]
                       for c in self.plist["connections"][self.filter["uid"]]}

    def test_modifiers_match_the_script(self):
        emitted = set(ec2.instance_item(inst("i-1"))["mods"])
        wired = {k for k, v in self.MOD_FLAGS.items() if v in self.routes}
        self.assertEqual(emitted, wired)

    def test_each_action_goes_to_the_right_object(self):
        self.assertEqual(self.routes, {
            0: "alfred.workflow.action.terminalcommand",        # ↩ SSM / sso login in iTerm
            1048576: "alfred.workflow.output.clipboard",        # ⌘↩ copy private IP
            524288: "alfred.workflow.action.openurl",           # ⌥↩ console
        })

    def test_script_filter_config(self):
        cfg = self.filter["config"]
        self.assertEqual(cfg["keyword"], "ec2")
        self.assertEqual((cfg["queuemode"], cfg["queuedelaymode"]), (2, 1))  # network settings
        self.assertEqual(cfg["script"], '/usr/bin/python3 -B ec2.py "$1"')
        terminal = [o for o in self.plist["objects"] if o["type"] == "alfred.workflow.action.terminalcommand"][0]
        self.assertEqual(terminal["config"], {"script": "{query}", "escaping": 0})

    def test_settings_are_configure_workflow_fields_not_plist_variables(self):
        # Users set these in Configure Workflow (saved to the gitignored prefs.plist). Plist
        # variables would ship in the repo and be overwritten on every update.
        self.assertFalse(self.plist.get("variables"))
        fields = {c["variable"]: c["config"]["default"] for c in self.plist["userconfigurationconfig"]}
        # Defaults must match the code's: empty = discover from ~/.aws/config, cache 300 s.
        self.assertEqual(fields, {"AWS_PROFILES": "", "AWS_REGIONS": "", "EC2_CACHE_SECONDS": "300"})
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(ec2.cache_seconds(), int(fields["EC2_CACHE_SECONDS"]))


if __name__ == "__main__":
    unittest.main()
