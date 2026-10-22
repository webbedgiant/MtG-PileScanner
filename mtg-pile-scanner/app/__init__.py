# Use the Windows certificate store (fixes HTTPS failures behind antivirus/proxy HTTPS inspection).
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass
