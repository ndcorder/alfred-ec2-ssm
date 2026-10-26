# EC2 SSM for Alfred

Find EC2 instances across all your AWS profiles and regions from Alfred, and open a Session Manager (SSM) shell on one in your terminal.

Profiles and regions are searched in parallel, and results are cached, so the list appears quickly after the first search. If one profile or region fails, its error is shown next to the instances that did load, with a hint about what to do.

## Usage

Type `ec2` and words to filter by. A row matches when every word is found in the name, instance id, private or public IP, or instance type. Running instances come first, then the rest by name.

| Key | Action |
|---|---|
| ↩ | Start an SSM session in your terminal (Alfred's Terminal setting). Running instances only. |
| ⌘ ↩ | Copy the private IP |
| ⌥ ↩ | Open the instance in the AWS console |
| ⌘ C | Copy the instance id |
| ⌘ L | Show details (state, type, IPs, platform, launch time) |

The subtitle shows the instance id, type, private IP, and the profile and region it was found in.

When an SSO profile has expired or missing credentials, its error row runs `aws sso login --profile <profile>` in your terminal on ↩.

## Configuration

Open the workflow in Alfred Preferences and click Configure Workflow.

| Setting | Default | Meaning |
|---|---|---|
| Profiles | empty | Comma-separated AWS CLI profiles. Empty means every profile from `aws configure list-profiles`. |
| Regions | empty | Comma-separated regions to search for every profile. Empty means each profile's own region from `~/.aws/config`. |
| Cache seconds | `300` | How long instance lists and profile and region lookups are cached |

Errors are never cached, so after `aws sso login` the next search tries again at once. To force a refresh, delete the files in Alfred's workflow cache folder for this workflow.

## What it calls in AWS

The workflow only reads. It runs these AWS CLI commands:

| Command | When |
|---|---|
| `aws configure list-profiles`, `aws configure get region` | To find profiles and regions (local, no AWS call) |
| `aws ec2 describe-instances` | Once per profile and region, then cached |
| `aws sts get-caller-identity` | Only when a region answers AuthFailure and the profile worked nowhere else. It tells a region that is not enabled for the account apart from bad credentials. The output (account id and ARN) is discarded. |

Nothing is started, stopped or changed. An SSM session starts only when you press ↩ on a running instance.

## Requirements

- macOS with Alfred 5 and the Powerpack
- `/usr/bin/python3` (installed with the Xcode Command Line Tools)
- AWS CLI v2. Alfred does not use your shell's PATH, so the workflow uses the first `aws` it finds in `/usr/local/bin` (official installer), `/opt/homebrew/bin`, `~/.local/bin` and `/usr/bin`, in that order.
- The Session Manager plugin for the AWS CLI, for SSM sessions
- Instances with the SSM agent and an instance profile that allows Session Manager, and credentials that allow `ec2:DescribeInstances` and `ssm:StartSession`
- Terminal actions use the app chosen in Alfred's Terminal setting (Terminal, iTerm or a custom script).

## Icons

Icons are drawn by `iconkit.js`, a small JavaScript for Automation script that uses AppKit, so there is nothing to install. The set is pre-rendered into `icons/`. After changing the icon table in `make_icons.py`, run `/usr/bin/python3 -B make_icons.py` to render it again.

| Icon | Meaning |
|---|---|
| Green server | Running |
| Grey stop | Stopped |
| Amber hourglass | Pending, stopping or rebooting |
| Red cross | Shutting down or terminated |
| Blue key | Run aws sso login for this profile |

## Install

Download the latest `.alfredworkflow` from Releases and open it.

To work on it, clone the repository and symlink it into Alfred's workflows folder:

```sh
git clone https://github.com/ndcorder/alfred-ec2-ssm.git
ln -s "$PWD/alfred-ec2-ssm" "<Alfred preferences>/Alfred.alfredpreferences/workflows/alfred-ec2-ssm"
```

Your Configure Workflow values are saved in `prefs.plist` in the workflow folder, which is ignored by git.

## Development

```sh
/usr/bin/python3 -B -m unittest test_ec2
```

Tests mock the AWS CLI and osascript and make no AWS calls.

## Licence

MIT. See [LICENSE](LICENSE).
