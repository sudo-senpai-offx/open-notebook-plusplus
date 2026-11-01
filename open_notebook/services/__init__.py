"""Onboard-Plus-Plus mini-services package.

Hosts the lightweight service modules that sit between the API and external
resources: browser automation (browser-use), quota tracking, and clipboard
sync (linuxdrop). All services are subprocess-friendly, rate-limit aware,
and designed to run on both ARM (phone) and x86 (PC).
"""
