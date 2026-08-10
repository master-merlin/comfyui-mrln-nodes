"""Node domains, one module per domain (image.py, mask.py, text.py, ...).

A domain module must export NODE_CLASS_MAPPINGS and
NODE_DISPLAY_NAME_MAPPINGS, and is activated by adding its module name to
`mrln.registry.DOMAINS`. Nothing in this package is imported implicitly.
"""
