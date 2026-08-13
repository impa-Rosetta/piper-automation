# Contributing

1. Keep hardware-free logic testable on Windows and Linux.
2. Do not commit factory trajectories, coordinates, logs, IP addresses, keys,
   passwords, or camera captures.
3. Use only documented `piper_sdk` APIs; do not implement raw CAN commands.
4. Any motion change must be validated unloaded and at low speed first.
5. Run `python -m unittest discover -s tests -v` before opening a pull request.
