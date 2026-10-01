# shield-manager

Deploy apps, packages, and updates to Nvidia Shield devices over network ADB.

shield-manager is a small home MDM: every Shield in the house mirrors the same set of apps.
It registers your Shields, picks one as the reference, reports how the others drift from it,
and syncs them to match. It can also install, update, remove and list apps on one Shield, a
group, or all of them, and it handles Shields of different models (different CPU types).

You can use it three ways, all with the same features:

- **In Home Assistant**, as an app in the sidebar (including the Home Assistant phone app).
- **In a web browser**, from `shield-manager web` running on a computer.
- **From the command line** on a computer.

The project is early: there is no released package yet, so every install below starts from
this repository's source.

## Contents

- [Before you start: prepare your Shields](#before-you-start-prepare-your-shields)
- [Set up in Home Assistant](#set-up-in-home-assistant)
  - [Install the app](#install-the-app)
- [Set up on a computer](#set-up-on-a-computer)
  - [Requirements](#requirements)
  - [Install](#install)
  - [Update](#update)
- [Use the web UI](#use-the-web-ui)
  - [Apps tab](#apps-tab)
  - [Screens tab](#screens-tab)
  - [Shields tab](#shields-tab)
  - [Install progress](#install-progress)
  - [Access and security](#access-and-security)
- [Use the command line](#use-the-command-line)
  - [Register Shields and groups](#register-shields-and-groups)
  - [Install, update and remove apps](#install-update-and-remove-apps)
  - [Mirror a reference Shield](#mirror-a-reference-shield)
  - [Check for updates](#check-for-updates)
  - [Choose GitHub sources](#choose-github-sources)
- [How apps are found for mixed Shield models](#how-apps-are-found-for-mixed-shield-models)
  - [Use a download link](#use-a-download-link)
- [Where your data is stored](#where-your-data-is-stored)
- [Development](#development)
  - [Release a new version](#release-a-new-version)
- [License](#license)

## Before you start: prepare your Shields

Do this once on every Shield, whichever way you run shield-manager.

1. Open **Settings → Device Preferences → About**.
2. Select **Build** seven times, until it says you are a developer.
3. Go back to **Device Preferences → Developer options** and turn on **Network debugging**.
4. Note the Shield's IP address. Network debugging shows it once it's on, and it's also under
   **Settings → Network & Internet** when you select your network.

The first time shield-manager connects to a Shield, the TV asks whether to allow debugging
from this computer. Accept it with the remote (tick **Always allow** so it doesn't ask again).

## Set up in Home Assistant

On Home Assistant OS, Shield Manager runs as a Home Assistant app (formerly add-on). Its
app-store screen then appears in the sidebar, including in the Home Assistant app on your
phone, and nothing has to run on a computer.

### Install the app

1. In Home Assistant, open
   [this link](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FGitJaxder%2Fshield-manager)
   and select **Open link**, then **Add**. Or do it by hand: go to **Settings → Apps**
   (**Add-ons** on older versions), open the app store, select **⋮ → Repositories**,
   paste `https://github.com/GitJaxder/shield-manager` and select **Add**.
2. Reload the app store page (**⋮ → Check for updates**), find **Shield Manager** and
   select it.
3. Select **Install**. Home Assistant builds the app, which takes a few minutes.
4. Turn on **Show in sidebar**, then select **Start**.
5. Open **Shield Manager** in the sidebar and add your Shields on the **Shields** tab. The
   app's **Documentation** tab walks through this.

If you already use Home Assistant's Android TV integration, the app reuses its ADB key, so
your Shields won't ask to allow debugging again. Your Shields and settings are kept in the
app's storage and included in Home Assistant backups. Settings from `shield-manager` on a
computer aren't copied over; add the Shields again in the app.

**Updating:** when a new version is released, Home Assistant offers it under **Settings →
Updates**. Each version installs the code of its own release, listed in the
[changelog](CHANGELOG.md).

The screen in the sidebar is the same web UI described in [Use the web UI](#use-the-web-ui).

## Set up on a computer

Use this if you don't run Home Assistant OS, or want the command line.

### Requirements

- A computer on the same home network as your Shields (Windows, macOS or Linux).
- Python 3.10 or newer. Check by running `python3 --version` (on Windows, `py --version`).
  If it's missing or older, install it from <https://www.python.org/downloads/>. On Windows,
  tick **Add python.exe to PATH** in the installer.
- Your Shields prepared as in
  [Before you start](#before-you-start-prepare-your-shields).

### Install

1. Get the source code. Use either option:
   - **Download:** on <https://github.com/GitJaxder/shield-manager>, click **Code → Download
     ZIP**. Unzip it. You'll get a folder called `shield-manager-main`.
   - **Git:** run `git clone https://github.com/GitJaxder/shield-manager.git`. You'll get a
     folder called `shield-manager`.

2. Open a terminal (on Windows, PowerShell) in that folder. For example:

   ```sh
   cd ~/Downloads/shield-manager-main
   ```

3. Create a private Python environment for the tool and switch to it.

   macOS / Linux:

   ```sh
   python3 -m venv .venv
   source .venv/bin/activate
   ```

   Windows (PowerShell):

   ```powershell
   py -m venv .venv
   .venv\Scripts\Activate.ps1
   ```

   If PowerShell says running scripts is disabled, run
   `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, then try again.

4. Install shield-manager and its dependencies:

   ```sh
   pip install .
   ```

5. Check that it works:

   ```sh
   shield-manager --version
   ```

   This should print `shield-manager 0.2.0`.

6. Add your first Shield, using the name you want and the IP address you noted:

   ```sh
   shield-manager device add living-room 192.168.1.20
   shield-manager device info living-room
   ```

   Accept the debugging prompt on the TV. `device info` then prints the Shield's model,
   Android version and CPU type.

The `shield-manager` command only works while the environment from step 3 is active. In a new
terminal, `cd` back into the folder and run the `activate` line from step 3 again.

From here, either start the web UI with `shield-manager web` and open
<http://127.0.0.1:8765/> (see [Use the web UI](#use-the-web-ui)), or carry on with
[Use the command line](#use-the-command-line).

### Update

1. Download the ZIP again and unzip it over the old folder, or run `git pull` in the folder.
2. In a terminal in that folder, activate the environment (step 3 above).
3. Run `pip install .` again.

Your Shields and settings are kept, because they live in `~/.config/shield-manager/`, not in
the source folder.

## Use the web UI

In Home Assistant, open **Shield Manager** in the sidebar. On a computer, run:

```sh
shield-manager web            # then open http://127.0.0.1:8765/
```

### Apps tab

An app-store-style screen listing every app on any of your Shields, with its name and icon
(read from the Shield), search, and filters for apps that are missing or out of date
somewhere.

- **Install or update one app:** tap it and pick a Shield. It's copied from a Shield that
  already has the newest version.
- **Several apps at once:** tick the checkbox on each app's icon. A bar appears with
  **Sync** (install or update them on every Shield that's missing or behind), **Remove…**
  (from the Shields you pick; none are picked for you), **Select all** and **Clear**.
- **Sync now:** a banner offers this when Shields have fallen behind the reference. It opens
  a preview of what each Shield would get, like `fleet status`. Tick the Shields to sync and,
  as with `fleet sync`'s options, choose whether to also downgrade apps that are newer than on
  the reference, remove apps the reference doesn't have, or download from GitHub. Sync never
  removes apps unless you choose that.
- **Updates from GitHub:** when the page opens (at most every 6 hours, or when you press
  **Check again**) it checks GitHub for newer versions of open-source apps. Apps with one show
  "Update available: 1.0 → 1.1" and appear under the **Updates** filter. Press **Update all**
  in the banner, or tick apps and press **Update**. This is the same as
  `fleet updates --install` (see [Check for updates](#check-for-updates)).
- **Get it another way** (on an app's page): open its Play Store page on a Shield's TV
  (`app store-page`), or find a download page for that Shield's CPU type
  (`app download-page`).
- **GitHub source** (on an app's page): set, edit or remove the repository the app's releases
  come from (owner/name or the repo's URL, plus an optional file-name pattern), like
  `source set` and `source remove`. The update check re-runs right after. Cards name the
  repo, and the **From GitHub** filter lists every app with a source (`source list`).

### Screens tab

What every Shield is showing right now: a screenshot, the app in the foreground, and whether
the Shield is asleep. It refreshes every 10 seconds while the tab is open. Protected video
(Netflix, Disney+ and most paid streaming apps) shows as black, but the app's name still shows.

### Shields tab

Add, group and remove Shields, pick the reference, check a connection, list each Shield's
apps (system apps too, if you tick the box), and install an APK or APK bundle (`.apkm`,
`.xapk`, `.apks`) you upload.

### Install progress

Installs run in the background, so closing the page doesn't stop them. While they run, a panel
at the bottom shows each app on each Shield moving through Downloading, Copying and
Installing, with a percentage where the transfer reports one, and which Shield it's copied
from. If an app can't be installed, its row lists what each source had (see
[How apps are found](#how-apps-are-found-for-mixed-shield-models)) with links to try next.

### Access and security

The web UI has no login. On a computer it listens on localhost only by default:

- `--host 0.0.0.0` exposes it to your network.
- `--allow-from IP` limits it to one client, such as a reverse proxy. That client is
  trusted to check who's asking, so give it a proxy, not a computer with a web browser.
- `--hostname NAME` lets the page be opened by a name such as `shield.lan`. Without
  `--allow-from`, the page only answers to its IP addresses, `localhost` and these names,
  so a website you visit can't reach it by pointing its own name at your network (DNS
  rebinding).
- `--port` changes the port (default `8765`).

The page uses only relative URLs, so it works behind a path prefix such as Home Assistant's
ingress. The Home Assistant app sets this up for you.

## Use the command line

All commands work on a computer after [Install](#install). `-d NAME`, `-g GROUP` and `--all`
pick target Shields and can be combined. Every command has `--help`.

### Register Shields and groups

```sh
shield-manager device add living-room 192.168.1.20
shield-manager device list
shield-manager device info living-room   # accept the debugging prompt on the TV the first time
shield-manager device remove living-room
```

Groups let one command reach several Shields:

```sh
shield-manager device add den 192.168.1.21 --group upstairs
shield-manager device set-groups living-room downstairs   # replace a device's groups
```

### Install, update and remove apps

```sh
shield-manager app install kodi.apk --all              # installs, or updates if already present
shield-manager app install steamlink.apkm -d bedroom   # APK bundles work too
shield-manager app install kodi.apk -g upstairs --allow-downgrade
shield-manager app version org.xbmc.kodi --all         # installed version per device
shield-manager app list -d living-room                 # third-party apps (--system for all)
shield-manager app uninstall org.xbmc.kodi -d den
```

`app install` takes an APK or an APK bundle: APKMirror's `.apkm`, `.xapk`, or bundletool's
`.apks`. From a bundle, each Shield gets the base APK plus the split for its own CPU type
(and the screen and language splits), so one file works on every Shield model. Bundles from
APKMirror that are encrypted (older uploads) can't be installed; download another version.

While it works, installs, removals and `fleet sync` show live progress such as
`den: Downloading org.xbmc.kodi - 40%`, then `Copying`, then `Installing`.

After an install the tool checks that each device reports the APK's `versionCode`. Devices are
handled one at a time; an unreachable device is reported and the rest still run, and the
command exits non-zero if any device failed.

### Mirror a reference Shield

Pick the Shield whose apps the others should match, then check and sync:

```sh
shield-manager fleet set-reference living-room
shield-manager fleet status              # exits 1 if any Shield differs
shield-manager fleet sync --dry-run      # what sync would change
shield-manager fleet sync                # copy missing and outdated apps from the reference
```

`sync` pulls each app's APK files (including Play Store split APKs) from the reference once
and installs them on every Shield that needs them. Shields of a different model get a build
for their own CPU type, as described in
[How apps are found](#how-apps-are-found-for-mixed-shield-models).

It never removes or downgrades anything unless asked:

- `--prune` removes apps the reference doesn't have (this deletes their data).
- `--allow-downgrade` replaces versions newer than the reference's.
- `--from NAME` uses another Shield as the reference for this run only.
- `-d`/`-g`/`--all` limit which Shields are checked.
- `--no-download` skips the GitHub and download-link steps.

Apps copied this way are sideloaded, so paid apps that check their Play Store licence may
refuse to run until they are installed from the Play Store on that Shield.

### Check for updates

```sh
shield-manager fleet updates             # list open-source apps with a newer version on GitHub
shield-manager fleet updates --install   # install those updates on every Shield
```

This compares each installed app that has a GitHub source (see
[Choose GitHub sources](#choose-github-sources)) with the newest release there. Other apps
can't be checked yet. Updates are installed on every Shield that has the app, with the build
for its CPU type, and only when the download is signed by the same developer as the installed
copy.

### Choose GitHub sources

Like Obtainium, you can tell shield-manager which GitHub repository publishes an app. Sync
then downloads it from there when no Shield has a copy a Shield can run, and
`fleet updates` checks it for newer versions. SmartTube and the Home Assistant app are
built in.

1. Find the app's package name. It's in `shield-manager app list -d living-room`, or in the
   app's Play Store web address after `id=`.
2. Find the repository that publishes the app's APK files under **Releases**, for example
   <https://github.com/yuliskov/SmartTube>.
3. Set it:

   ```sh
   shield-manager source set org.example.app https://github.com/example/app
   ```

   If the repository publishes several apps or variants, add `--asset` with a pattern the
   right files' names match, for example `--asset tv`. The pattern is a regular expression
   and ignores upper and lower case.

4. Check it:

   ```sh
   shield-manager source list
   ```

`shield-manager source remove org.example.app` stops using GitHub for that app. It works for
the built-in ones too. Downloads are still only installed when they're signed by the same
developer as the copy on your Shields.

## How apps are found for mixed Shield models

Different Shield models run different CPU types, and the Play Store gives each Shield only
the version built for its own. So for each Shield, sync, installs and updates (in Home
Assistant, the web UI and the command line alike) work down this list and stop at the first
step that gives a copy that Shield can run:

1. **Your Shields:** a copy from another Shield, trying Shields of the same model first.
2. **GitHub:** a release, for open-source apps with a GitHub source (SmartTube and the Home
   Assistant app are built in). It's only installed when it's signed by the same developer as
   the copy already on your Shields, so a tampered download is refused.
3. **APKMirror:** a link to the page for the right version and CPU type, found the way
   [Morphe](https://github.com/MorpheApp/morphe-manager) does. Morphe's server occasionally
   finds it on Uptodown, APKPure or APKCombo instead.
4. **Google search:** when there's no such page, a link to a web search on those sites.
5. **Play Store:** a command that opens the app's page on that Shield, where you press
   **Install** with the remote.

Steps 1 and 2 happen automatically. If neither works, the error lists what each step found,
for example:

```text
com.valvesoftware.steamlink: FAILED: no copy bedroom can run.
    1. Your Shields (this one runs armeabi-v7a, armeabi): the copies on den are built for a different CPU type
    2. GitHub: it isn't an open-source app it knows the repository of
    3. APKMirror: download it from https://www.apkmirror.com/apk/..., then install the file with `shield-manager app install FILE -d bedroom`
    4. Play Store: `shield-manager app store-page com.valvesoftware.steamlink -d bedroom` opens it
```

The web UI shows the same list on the failed app's row, with the links as buttons.

### Use a download link

1. Open the link on your computer or phone.
2. Download the file. On APKMirror, scroll to the download section and press
   **DOWNLOAD APK**, or **DOWNLOAD APK BUNDLE** for apps that come as a bundle (an `.apkm`
   file). Either works.
3. Install it on the Shield. Either upload it on the web UI's **Shields** tab, or run:

   ```sh
   shield-manager app install ~/Downloads/the-file.apkm -d bedroom
   ```

Files you download yourself aren't checked against your Shields' copy, so stick to the page
the link opens. You can also get the link without syncing:

```sh
shield-manager app download-page com.valvesoftware.steamlink -d bedroom
```

To open the Play Store page on the Shield's TV instead:

```sh
shield-manager app store-page com.valvesoftware.steamlink -d bedroom
```

## Where your data is stored

On a computer, everything lives in `~/.config/shield-manager/` (set `SHIELD_MANAGER_HOME` to
use another folder):

- `devices.json`: your Shields, groups and the reference Shield.
- `adbkey` and `adbkey.pub`: the ADB key, created on first use. Your Shields trust this key.
- `app-sources.json`: the GitHub sources you set.
- `app-cache/`: app names and icons read from your Shields.

In Home Assistant, the same files live in the app's own storage and are included in Home
Assistant backups.

## Development

Follow [Install](#install), but in step 4 run `pip install -e ".[dev]"` instead, so code
changes take effect without reinstalling and the test tools are installed. Then:

```sh
ruff check . && ruff format --check .
pytest
```

### Release a new version

Home Assistant offers the app's new version as soon as `ha-app/shield_manager/config.yaml`
on `main` has it, and the app installs the code of the release tag that matches that version.

1. In a pull request, set the new version (for example `0.3.0`) in all three places:
   `pyproject.toml`, `src/shield_manager/__init__.py` and
   `ha-app/shield_manager/config.yaml`. Add a `## 0.3.0` section at the top of
   `CHANGELOG.md`. CI fails if any of these disagree.
2. Merge the pull request.
3. On GitHub, open **Releases → Draft a new release**.
4. Select **Choose a tag**, type `v0.3.0` (a `v`, then the version), and select
   **Create new tag: v0.3.0 on publish**. Leave the target as `main`.
5. Set the title to `v0.3.0` and paste that version's changelog section as the description.
6. Select **Publish release**.

Publish right after merging. Until the tag exists, installing or updating the app in Home
Assistant fails, because it looks for the code of a release that isn't out yet.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
