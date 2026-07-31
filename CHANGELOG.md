# Changelog

All notable changes to this project are documented here.

## v1.2.2 - 2026-07-31

### Fixed

- Kiro CLI browsing no longer ignores the current workspace when `kiro-md.py`
  sits in the directory it is run from. That is the normal case for the
  documented quick start, and it silently disabled workspace auto-selection,
  so every session from every workspace was listed at once.
- Auto-selection from a parent directory is now unambiguous. Running from a
  folder that contains several workspaces keeps the ALL view instead of
  silently scoping to an arbitrary one.

### Changed

- The workspace recap collapses to a single line once a workspace is scoped,
  since the header already shows its full path.

## v1.2.1 - 2026-06-21

### Added

- Full session IDs beneath every Kiro IDE and Kiro CLI session row.
- The latest full session ID for each workspace in the workspace picker.

### Changed

- Renamed the numeric session-selection column from `ID` to `#` so it cannot be
  confused with the actual Kiro session ID.

## v1.2.0 - 2026-06-13

### Added

- Kiro CLI session browsing alongside Kiro IDE browsing.
- Startup source chooser: IDE sessions, CLI sessions, or Find by session ID.
- Parallel session-ID search across Kiro IDE and Kiro CLI storage.
- Full Kiro CLI `.jsonl` event-stream parsing for rich exports.
- CLI helper/subagent session toggle.
- Workspace highlight and current-workspace auto-selection.
- CLI message-count column and real transcript-size display.
- Save-location prompt for file exports: project directory or script directory.
- Multi-workspace save behavior for separate exports.

### Changed

- CLI export now prefers the detailed `.jsonl` stream over the compact `.json`
  index when both are present.
- CLI sessions now export file reads, file creates, file edits, terminal
  commands, terminal outputs, code search, MCP calls, web activity, subagent/task
  calls, compactions, and errors.
- The README now documents both Kiro IDE and Kiro CLI workflows.

### Notes

- Kiro CLI `.json` files are compact metadata. The detailed transcript is stored
  in the matching `.jsonl` file.
- The tool remains local and read-only against Kiro storage. It writes only the
  Markdown exports selected by the user.

## v1.1.x

### Added

- Kiro IDE compaction-chain detection.
- Chain merge export.
- Interactive section filtering with presets and output caps.
- Clean-chat mode for stripping IDE context noise.
- Faster Kiro IDE execution-record indexing.
