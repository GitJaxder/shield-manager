# Shield Manager

Keeps the apps on your Nvidia Shields in sync and gives you an app-store screen in the
Home Assistant sidebar, including in the Home Assistant app on your phone.

## Before you start

Turn on network debugging on each Shield:

1. Open **Settings → Device Preferences → About**.
2. Select **Build** seven times, until it says you are a developer.
3. Go back to **Device Preferences → Developer options** and turn on **Network debugging**.
4. Note the Shield's IP address. It's under **Settings → Network & Internet** when you
   select your network.

## Adding your Shields

1. Open **Shield Manager** in the sidebar.
2. Go to the **Shields** tab and add each Shield with a name (for example `living-room`)
   and its IP address, then press **Add Shield**.
3. Press **Check** next to each one. If the TV asks **Allow network debugging?**,
   tick **Always allow from this computer** and select **OK**.

The first Shield you add is the reference: the one the others copy their apps from. You
can pick another one on the **Shields** tab.

## Options

**ADB key** (optional): the app makes its own ADB key, so each Shield asks once whether to
allow it (step 3 above). If Home Assistant's Android TV integration already controls your
Shields, you can give the app that integration's key instead, and they won't ask again:

1. Install the **Terminal & SSH** app, open it, and run
   `cat /homeassistant/.storage/androidtv_adbkey`.
2. Copy everything it prints, from `-----BEGIN PRIVATE KEY-----` to
   `-----END PRIVATE KEY-----`.
3. In **Settings → Apps → Shield Manager → Configuration**, paste it into **ADB key**,
   press **Save**, and restart the app.

The app doesn't read Home Assistant's own files, so it can't see your other integrations'
passwords and tokens. Clearing the option keeps the key the app is using.

## Your data

Your Shields, settings, ADB key and app icons are kept in the app's own storage, so they
survive restarts and updates and are included in Home Assistant backups.
