"""Reliability for daily use (build step 18): one instance only, crash and sleep recovery,
full backups with restore, health report, low-resource and on-battery guards, owner settings.

Everything here lives in the HCWMA data folder (`memory.config.resolve_data_dir`), outside
the repository, and is used only by the owner's app and command line — no agent tool can
reach it.
"""
