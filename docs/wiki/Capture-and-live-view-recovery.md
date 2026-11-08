# When the live image stops updating

The exposure, processing of the image, and the browser's connection are independent. A camera connected directly to the capture host can keep recording through a Wi-Fi outage. An INDI camera on a remote host still needs a working network path to its driver.

For a Wi-Fi-only Pi that travels between networks, see [Wi-Fi recovery](Wi-Fi-Recovery.md). The optional helper uses NetworkManager independently of the capture service.

* If the companion reports a lost connection, leave the window open. It retains the last decoded frame and checks again when the connection returns or the window becomes visible. A chart failure need not interrupt the live image.
* If the Pi responds but the image is old, compare the last stored capture with the current time. Allow for exposure, the configured capture interval and processing. A scheduled pause or disabled daytime capture is expected.

The live view shows the age and camera-local time of the picture on screen. Its age keeps counting up while the next picture downloads. Once the new image is visible, the time and age refer to that frame. The short line at the bottom of the sidebar stays visible when the Status section is closed. On other pages it reports the latest available image for the selected camera.

If an update stalls, the live view shows the reason when the camera can provide one. The browser retries interrupted requests automatically. **Retry now** requests the image again. For a paused capture or a service issue, the page links to the relevant settings or service controls when you have access.
* If a new image is being loaded, the displayed timestamp still describes the visible image. It advances when the replacement is ready to display.

On a source installation, `systemctl --user status indi-allsky.service gunicorn-indi-allsky.service` separates the capture service from the web application. In the default SQLite installation you can inspect the capture supervisor's status without modifying the database:

```sh
sqlite3 -readonly /var/lib/indi-allsky/indi-allsky.sqlite \
  "SELECT value FROM state WHERE key = 'CAPTURE_HEALTH';"
```

`raw_received` and `image_published` are Unix timestamps: the first means the driver supplied a nonempty file, the second that the image worker made a complete live image available. The `updated` timestamp tells you how recent the status itself is. The capture supervisor measures elapsed time independently of NTP corrections. A limited recovery of the capture service does not reboot the host or restart an independent UPS service.

* A failed local web interface can recover independently. See [Web service recovery](Web-service-recovery.md).
* If a configured sensor cannot provide a valid sample, its driver retries independently. Both the standard sensor charts and the companion leave a gap for a failed reading and show a measured zero. The local status panel can show archive warnings and stale UPS readings to administrators. An optional local UPS monitor retries its I²C connection after repeated read failures.
* The archive monitor checks the configured image directory, its expected mount, free blocks and inodes, and an actual write. When space runs low, scheduled video and export jobs wait in the queue. When the archive is unavailable or full, capture pauses too. Checks continue and pending jobs resume in batches. The previously verified mount and entries in `/etc/fstab` protect a missing external disk. If you use a manually mounted volume, you can set `INDI_ALLSKY_ARCHIVE_MOUNT` to the expected mount point before starting the service for the first time.
* The video worker checks completed local capture dates for missing or damaged products. It checks failed movies first, then recent dates, and rotates through older dates while their source images and generated media are still within retention. It repairs one expensive batch at a time. Regular movies use the original exposures. Panorama movies use the recorded panorama frames for the same camera, date and time of day.
* Missing keograms and eligible star-trail products can be rebuilt without replacing valid products from the same period. Daytime and panorama settings, frame exclusions, skip counts, and the minimum frame count for a star-trail video still apply. A movie is published after a frame decodes successfully; replacing a failed movie retains its watch URL. The job log records the result when no suitable sources are available.
* Explicitly deleted products are excluded from subsequent repair. An interrupted repair can retry within a bounded budget. If the archive becomes unavailable, work waits for storage recovery.
