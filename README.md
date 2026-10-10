<div align="center">
  <a href="https://github.com/filippofinke/sideloop">
    <img width="140px" src=".github/logo.png" alt="Sideloop" />
  </a>
  <h3 align="center">Sideloop</h3>
</div>

> Keep sideloaded IPAs signed with a free Apple ID, re-installed over Wi-Fi before the 7-day signature expires.

A small self-hosted service with a web UI for any Linux machine (amd64 or arm64) or a Mac. It re-signs your apps with [AltServer-Linux](https://github.com/jaakkopalvaila/AltServer-Linux) and installs them in place, so app data is kept. You don't need AltStore or SideStore on the device.

## Features

- [x] Multiple apps on multiple iPhones and iPads
- [x] Automatic re-signing when a device comes online, before the signature expires
- [x] Re-signing away from home over your own WireGuard server
- [x] Web UI for pairing, uploading IPAs, 2FA codes and live progress
- [x] Checks each IPA for FairPlay encryption, tweaks and app extensions
- [x] Self-hosted Apple sign-in via [anisette-v3-server](https://github.com/Dadoum/anisette-v3-server)
- [x] Python standard library only, one Docker image for amd64 and arm64

## Demo

https://github.com/user-attachments/assets/8c6b799d-b9e7-4a15-898d-d96a2af8e876

A two-minute narrated walkthrough: setup, adding devices and apps, the first install with a 2FA code, and automatic re-signing.

## Screenshots

| Login | Apps | Signing | Settings | Activity |
| :---: | :---: | :---: | :---: | :---: |
| <img src=".github/screenshots/login.png" width="160" /> | <img src=".github/screenshots/apps.png" width="160" /> | <img src=".github/screenshots/signing.png" width="160" /> | <img src=".github/screenshots/settings.png" width="160" /> | <img src=".github/screenshots/activity.png" width="160" /> |

## Quick Start

Prerequisites

- Docker with Compose
- A throwaway Apple ID

Run on Linux

```bash
git clone https://github.com/filippofinke/sideloop.git
cd sideloop
cp .env.example .env
docker compose up -d
```

The first build compiles AltServer, which takes a few minutes. On a small board like a Raspberry Pi 3 it is much faster to build on another arm64 machine and copy the image over:

```bash
docker build --platform linux/arm64 -t sideloop .
docker save sideloop | gzip | ssh pi@<host> 'gunzip | docker load'
ssh pi@<host> 'cd sideloop && docker compose up -d --no-build'
```

Open `http://<host>:8080` and set a password. Then pair a device over USB, add an IPA and enter your Apple ID.

Run on macOS

Docker on macOS can't see USB or Wi-Fi devices, so bridge the system usbmuxd first:

```bash
socat TCP-LISTEN:27015,bind=127.0.0.1,reuseaddr,fork UNIX-CONNECT:/var/run/usbmuxd &
docker compose -f docker-compose.yml -f docker-compose.mac.yml up -d
```

Pair the device in Finder and enable **Show this iPhone when on Wi-Fi**.

## Notes

- Use the Apple ID's **regular password**. App-specific passwords don't work.
- Apple asks for a **2FA code** on the first sign-in. After that, sign-ins are silent.
- The device must be **unlocked and on the same Wi-Fi** while a re-sign runs, or reachable over [WireGuard](#away-from-home).
- A free account allows 3 apps per device and 10 App IDs per week. Each app extension needs its own App ID.
- Trust the developer once in **Settings > General > VPN & Device Management**. Refreshes keep it trusted.
- If Apple sign-in stops working, update `ALTSERVER_TAG` in the `Dockerfile`. AltServer is built from that tag with the patches in `altserver/`.

Data is stored in `./data`.

## Away from home

Sideloop can reach a device through a WireGuard server you already run, such as [wg-easy](https://github.com/wg-easy/wg-easy). Bonjour doesn't cross a VPN, so each device gets the IP it has in WireGuard. The device has to be on a Wi-Fi network: over mobile data alone it can't be refreshed. Linux only.

1. Create a WireGuard client for the device and import it in the WireGuard app. Note its IP, like `10.8.0.2`.
2. Route the WireGuard subnet. If wg-easy runs in Docker, give it a fixed `ipv4_address` on its network and set these on the `app` service:

   ```yaml
   environment:
     WG_SUBNET: 10.8.0.0/24     # wg-easy's IPv4 CIDR
     WG_GATEWAY: 172.18.0.2     # wg-easy's IP on its Docker network
   ```

   Sideloop then runs `ip route replace 10.8.0.0/24 via 172.18.0.2`. If WireGuard runs on the host or your router, leave them out and route the subnet there.
3. In wg-easy's **Admin Panel > Hooks**, add this to PostUp with the subnet of wg-easy's Docker network (`docker network inspect <network>` shows it), and the same line with `-D` instead of `-A` to PostDown. The device then sees sideloop as `10.8.0.1`:

   ```sh
   iptables -t nat -A POSTROUTING -s 172.18.0.0/16 -o wg0 -j MASQUERADE;
   ```

4. In **Settings > Away From Home**, enter the device's WireGuard IP.
5. In the WireGuard app, edit the tunnel and turn on **On-Demand** for Wi-Fi except your home network, so it only runs when you're out.
6. In Shortcuts, add an automation such as **App > Is Opened** for an app you use daily. Set it to **Run Immediately** and add **Get Contents of URL** with the device's link from **Copy Link**. The link uses the address you opened sideloop at, so copy it from an address the device can reach through WireGuard.

Notes

- iOS only answers while the device is on **some Wi-Fi network**, such as a hotel, an office or another phone's hotspot. On mobile data alone, refreshing doesn't work.
- With Persistent Keepalive off, the NAT of most networks drops the tunnel's return path soon after the device goes quiet, so sideloop can usually reach it only right after it sent something. The check-in Shortcut from step 6 does that, which makes it the most reliable way to start a refresh while you're away, so trigger the Shortcut from an app you open every day. A keepalive of 25 seconds keeps the device reachable, at some battery cost.
- Battery: nothing stays connected. Sideloop only contacts the device while a signature is due, on a check-in or with one short connection attempt every 30 minutes, until the refresh succeeds.
- Each link is tied to one device and stops working if `data/ui.json` is deleted.

## Author

👤 **Filippo Finke**

- Website: [https://filippofinke.ch](https://filippofinke.ch)
- Twitter: [@filippofinke](https://twitter.com/filippofinke)
- GitHub: [@filippofinke](https://github.com/filippofinke)
- LinkedIn: [@filippofinke](https://linkedin.com/in/filippofinke)

## Show your support

Give a ⭐️ if this project helped you!

<a href="https://www.buymeacoffee.com/filippofinke">
  <img src="https://github.com/filippofinke/filippofinke/raw/main/images/buymeacoffe.png" alt="Buy Me A McFlurry">
</a>

## 📝 License

Copyright © 2026 [Filippo Finke](https://github.com/filippofinke).<br />
This project is [MIT](./LICENSE) licensed. Icons from [Ionicons](https://ionic.io/ionicons) (MIT).

***

_Not affiliated with Apple. Use a throwaway Apple ID._
