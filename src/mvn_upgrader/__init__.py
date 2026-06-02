"""maven-dep-upgrader: automated Maven dependency & plugin upgrader.

Reads a Maven repo's effective POMs, checks Nexus for newer allowed versions,
upgrades one artifact at a time with a Codex auto-fix build loop, and opens a
GitLab merge request with a generated report.
"""

__version__ = "0.1.0"
