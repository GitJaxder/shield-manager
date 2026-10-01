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

**Use the Android TV integration's ADB key** (on by default): when the app first starts,
it copies the key Home Assistant's Android TV integration uses, so Shields that already
trust Home Assistant don't ask again in step 3. Turning it off, or not having that
integration, means the app makes its own key. The key is only copied once; to switch,
uninstall and reinstall the app.

## Sensors, buttons and automations

Install the Shield Manager integration as well (see the
[README](https://github.com/GitJaxder/shield-manager#home-assistant)). It finds this app
by itself and adds, for each Shield, how many apps it has and how many it's behind the
reference on, plus buttons to sync, and actions for automations.

## Your data

Your Shields, settings, ADB key and app icons are kept in the app's own storage, so they
survive restarts and updates and are included in Home Assistant backups.
