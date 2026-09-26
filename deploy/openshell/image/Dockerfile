# E2E-05 stand-in role image for the OpenShell VM driver (synthetic data only).
# Built with docker and flattened by "docker export" into a rootfs tar, the documented
# rootfs source for the VM compute driver. See deploy/openshell/build_rootfs.sh.
FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

# Host-originated synthetic sentinel. World-readable on purpose: Unix permissions allow
# the sandbox user to read it, so only the OpenShell filesystem policy can deny access.
COPY srv/rfa-private/SENTINEL_E2E05.txt /srv/rfa-private/SENTINEL_E2E05.txt
# Harmless executable outside every approved role path (out-of-scope execution probe).
COPY opt/rfa-unapproved/unapproved_tool.sh /opt/rfa-unapproved/unapproved_tool.sh
RUN chmod 0755 /srv/rfa-private /opt/rfa-unapproved \
 && chmod 0644 /srv/rfa-private/SENTINEL_E2E05.txt \
 && chmod 0755 /opt/rfa-unapproved/unapproved_tool.sh
