# Unifi Protect for Home Assistant
Unifi Protect Integration for Home Assistant
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/custom-components/hacs)

This is a Home Assistant Integration for Ubiquiti's Unifi Protect Surveillance system.

This Home Assistant integration is inspired by [danielfernau's video downloader program](https://github.com/danielfernau/unifi-protect-video-downloader) and the Authorization implementation is copied from there.

Basically what this does, is integrating the Camera feeds from Unifi Protect in to Home Assistant, and furthermore there is an option to get Binary Motion Sensors and Sensors that show the current Recording Settings pr. camera.

## Prerequisites
Before you install this Integration you need to ensure that the following two settings are applied in Unifi Protect:
1. **Local User needs to be added** Open Unifi Protect in your browser. Click the USERS tab and you will get a list of users. Either select and existing user, or create a new one. The important thing is that the user is part of *Administrators* and that a local username and password is set for that user.This is the username and password you will use when setting up the Integration later.<br>
2. **RTSP Stream** Select each camera under the CAMERAS tab, click on the camera and you will get a menu on the right side. Click the MANAGE button and there will be a menu like the picture below. (If you can't see the same picture click the + sign to the right of RTSP). Make sure that at least one of the streams is set to on. It does not matter whcich one, or if you select more than one. The integration will pick the one with the highest resolution.<br>

![USER Settings](https://github.com/briis/unifiprotect/blob/master/images/setup_user.png) ![RTSP Settings](https://github.com/briis/unifiprotect/blob/master/images/setup_rtsp.png)

## Manual Installation
To add Unifi Protect to your installation, create this folder structure in your /config directory:

`custom_components/unifiprotect`.
Then, drop the following files into that folder:

```yaml
__init__.py
manifest.json
sensor.py
binary_sensor.py
camera.py
protectnvr.py
```

## HACS Installation
This Integration is not yet part of the default HACS store, but will be once I am convinced it is stable. But it still supports HACS. Just go to settings in HACS and add `briis/unifiprotect` as a Custom Repository. Use *Integration* as Category.

