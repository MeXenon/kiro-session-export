# Changelog

All notable changes to this project are documented here.

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
