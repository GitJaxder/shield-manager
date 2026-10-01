# Changelog

Each version is released as a GitHub release tagged `v<version>`. Home Assistant's app
store offers the app's new version once it's released.

## 0.2.1

### Security

- Installing a split app or an APK bundle (.apkm, .xapk, .apks) could run commands on the
  Shield: the file names inside the bundle were passed to the Shield's shell as they were.
  A bundle someone tampered with, uploaded or downloaded from GitHub, could use that. They
  are now cleaned and quoted.

## 0.2.0

### Removed

- The Shield Manager integration for Home Assistant. The app in the sidebar does
  everything it did. If you installed the integration, delete it under **Settings →
  Devices & services → Shield Manager → ⋮ → Delete**, then remove it in HACS.

### Changed

- The app no longer announces itself to Home Assistant, and only accepts connections from
  the sidebar page.

## 0.1.0

The first release.

### Home Assistant

- **Shield Manager app** (formerly add-on): runs the web UI inside Home Assistant OS and puts it
  in the sidebar, including in the Home Assistant phone app. It reuses the Android TV
  integration's ADB key, so Shields that already trust Home Assistant don't ask again.
- **Shield Manager integration** (HACS): finds the app by itself. Each Shield gets **Apps
  installed**, **Apps behind reference**, **Reachable** and a **Sync from reference** button.
  The integration also adds a **Reference Shield** picker, an **Activity** sensor, buttons to
  sync all Shields and to check for and install app updates, and the `shield_manager.sync`
  and `shield_manager.install` actions.

### Web UI

- An app-store screen listing every app on any Shield, with names, icons, search and
  filters. Tap an app to install or update it on a Shield, or tick several apps to sync or
  remove them together.
- **Sync now** previews and copies what each Shield is missing from the reference Shield.
- Live progress for each app on each Shield: Downloading, Copying and Installing.
- A **Screens** tab showing what each Shield is displaying.
- Install an APK or APK bundle (`.apkm`, `.xapk`, `.apks`) you upload.
- Checks GitHub for newer versions of open-source apps and installs them.

### Command line

- Register Shields and groups, then install, update, list and remove apps on one Shield, a
  group or all of them.
- `fleet status` and `fleet sync` mirror the apps of a reference Shield. Sync never removes
  or downgrades unless asked to.
- Works across Shield models with different CPU types. It copies from a Shield that can run
  the app, then tries GitHub, then gives an APKMirror link, then the Play Store page.
- `source set` picks the GitHub repository an app's releases come from, like Obtainium.
