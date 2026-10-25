# Input

Drop one requirements file per feature or app here, as `<name>.md`.

When the project's trigger fires (a scheduled scan, a watched-folder change, or **Run now**), Cortana moves
it to `runs/<id>/requirements.md` and starts the pipeline. Scheduled scans leave a file alone until it has
been unchanged for 60 seconds. This README and files starting with `.` or `_` are ignored (use
`_draft.md` for work in progress).
