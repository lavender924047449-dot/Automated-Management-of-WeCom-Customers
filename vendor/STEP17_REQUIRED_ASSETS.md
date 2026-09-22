# Step 17 Required Assets

Step 17 packaging expects these local files before running `build/release.ps1`:

- `vendor/frida-server-16.7.19-android-x86_64` (required)
- `vendor/adb.exe` (required)
- `vendor/scrcpy/` (optional)

## Notes

- `build/release.ps1` stops with an error if required assets are missing.
- At runtime, GUI startup tries to:
  - run `adb connect 127.0.0.1:16384`
  - push `frida-server` to `/data/local/tmp/frida-server`
  - restart `frida-server` on the Android side

