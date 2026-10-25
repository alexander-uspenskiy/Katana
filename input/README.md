# Input

Drop one requirements file per feature or app here, as `<name>.md`.

Within 10 minutes the orchestrator picks it up, moves it to `runs/<id>/requirements.md` and starts the
pipeline. Files are left alone until they have been unchanged for 60 seconds, and this README and files
starting with `.` or `_` are ignored (use `_draft.md` for work in progress).
