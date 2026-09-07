# QGIS Plugin Repository — Flagged Issues (2026-09-06)

## Bandit Security (78 issues, 36/36 rules enabled)

| Code | Rule | Description |
|------|------|-------------|
| B101 | Assert used | assert removed in optimised mode (-O) |
| B104 | Hardcoded bind all interfaces | Binding to 0.0.0.0 |
| B108 | Hardcoded temp directory | Predictable temp paths (TOCTOU risk) |
| B110 | Try/except/pass | Silently swallowing exceptions |
| B112 | Try/except/continue | Silently continuing after exception |
| B113 | Request without timeout | HTTP request without timeout |
| B303 | MD5/SHA1 hash function | Insecure hash algorithms |
| B308 | Mark_safe used | Django mark_safe XSS risk |
| B310 | urllib urlopen | No SSL verification by default |
| B311 | Random module used | Not suitable for cryptographic use |
| B313 | XML bad cElementTree | XXE / injection risk |
| B314 | XML bad ElementTree | XXE / injection risk |
| B315 | XML bad expatreader | XXE risk |
| B316 | XML bad expatbuilder | XXE risk |
| B317 | XML bad sax | XXE risk |
| B318 | XML bad minidom | XXE risk |
| B319 | XML bad pulldom | XXE risk |
| B320 | XML bad etree | lxml.etree XXE risk |
| B324 | Insecure hash function (hashlib) | hashlib.md5/sha1 aliases |
| B403 | Import pickle | Arbitrary code on deserialise |
| B405 | Import xml.etree | XXE risk |
| B406 | Import xml.sax | XXE risk |
| B407 | Import xml.dom.expatbuilder | XXE risk |
| B408 | Import xml.dom.minidom | XXE risk |
| B409 | Import xml.dom.pulldom | XXE risk |
| B504 | SSL with no version | No minimum TLS version |
| B508 | SNMP insecure version | SNMPv1/v2c plaintext |
| B509 | SNMP weak cryptography | noAuthNoPriv / authNoPriv |
| B603 | subprocess without shell=True | Args as list (review required) |
| B606 | Start process with no shell | os.execl etc. |
| B607 | Start process with partial path | Executable replacement risk |
| B608 | Hardcoded SQL expression | SQL injection via string construction |
| B614 | PyTorch unsafe load | torch.load without weights_only=True |
| B702 | Mako templates used | XSS — no auto-escaping |
| B703 | Django mark_safe | mark_safe() / SafeData XSS |
| B704 | MarkupSafe Markup XSS | markupsafe.Markup() XSS |

## Flake8 Quality (96 issues, 20/20 rules enabled)

| Code | Rule |
|------|------|
| C901 | Function too complex (McCabe > 10) |
| E101 | Mixed spaces and tabs in indentation |
| E711 | Comparison to None using == / != |
| E712 | Comparison to True/False using == / != |
| E713 | Should use `not in` |
| E714 | Should use `is not` |
| E721 | Type comparison using == (use isinstance) |
| E722 | Bare except clause |

## File Analysis (4 issues, 2/2 rules enabled)

| Code | Rule |
|------|------|
| FILE_BINARY | Binary file found in plugin archive |
| FILE_EXECUTABLE | File with executable bit set |
