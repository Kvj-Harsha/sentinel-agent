# Read-only inspection of the effective root build context, not a production image.
# Requires the existing local Sentinel image for its Python interpreter.
FROM sentinel-agent:local
USER 0
RUN --mount=type=bind,target=/context,readonly python /context/scripts/audit_build_context.py /context
