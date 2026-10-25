"""Pure, source-aware checks for Apple configuration-profile common keys.

These checks use Apple's TopLevel and CommonPayloadKeys profile definitions.
They do not validate individual payload schemas or certify MDM deployment.
Unknown app/vendor keys and opaque encrypted content are preserved.
"""
import json
import html
import plistlib
import re
import uuid
from dataclasses import dataclass

try:
    from .plist_core import xml_bytes
except ImportError:
    from plist_core import xml_bytes


INTUNE_MAX_BYTES = 1000000
COMMON_KEYS = {
    "PayloadType": {"type": "string", "description": "Configuration at the profile root; the payload domain in each child."},
    "PayloadIdentifier": {"type": "string", "description": "Stable identifier, unique within this profile. Preserve existing identifiers when editing."},
    "PayloadUUID": {"type": "string", "description": "Globally unique payload identifier. Preserve existing values when editing."},
    "PayloadVersion": {"type": "integer", "description": "Common payload format version; must be the integer 1."},
    "PayloadDisplayName": {"type": "string", "description": "Human-readable profile or payload name."},
    "PayloadDescription": {"type": "string", "description": "Human-readable explanation of the profile or payload."},
    "PayloadOrganization": {"type": "string", "description": "Organization providing the profile or payload."},
    "PayloadContent": {"type": "array", "description": "At the profile root, an array of payload dictionaries."},
    "PayloadScope": {"type": "string", "description": "macOS profile scope: System (default) or User. The MDM deployment channel is configured separately."},
    "PayloadRemovalDisallowed": {"type": "boolean", "description": "Whether the profile disallows user removal; effective behavior depends on deployment."},
    "EncryptedPayloadContent": {"type": "data", "description": "Opaque encrypted payload data, used in place of root PayloadContent."},
}


@dataclass(frozen=True)
class ProfileIssue:
    severity: str
    message: str
    path: str
    offset: int


@dataclass(frozen=True)
class ProfilePayload:
    path: str
    offset: int
    display_name: object
    payload_type: object
    identifier: object
    uuid: object
    version: object


def _key_path(path, key):
    return path + "[" + json.dumps(key, ensure_ascii=False) + "]"


def _nodes(document):
    return {node.path: node for node in document.entries if node.tag not in ("key", "plist")}


def is_profile(document):
    """Recognize an XML profile envelope with Configuration payload type."""
    return isinstance(document.value, dict) and document.value.get("PayloadType") == "Configuration"


def profile_payloads(document):
    """List immediate payload dictionaries, exposing only common scalar metadata."""
    root = document.value
    content = root.get("PayloadContent") if isinstance(root, dict) else None
    if not isinstance(content, list):
        return []
    nodes = _nodes(document)
    result = []
    for index, payload in enumerate(content):
        if not isinstance(payload, dict):
            continue
        path = '$["PayloadContent"][{}]'.format(index)

        def string(key):
            value = payload.get(key)
            return value if isinstance(value, str) else None

        version = payload.get("PayloadVersion")
        result.append(ProfilePayload(
            path, nodes[path].start, string("PayloadDisplayName"),
            string("PayloadType"), string("PayloadIdentifier"), string("PayloadUUID"),
            version if type(version) is int else None,
        ))
    return result


def _uuid_identity(value):
    # Apple requires uniqueness rather than RFC formatting. Normalize UUIDs only
    # for duplicate detection; arbitrary identifiers and MDM tokens remain valid.
    try:
        return ("uuid", uuid.UUID(value).hex)
    except ValueError:
        return ("text", value)


def analyze_profile(document, target="generic"):
    """Check common profile structure; return issues at Unicode source offsets.

    target is generic, intune, or jamf. This never rewrites identifiers, evaluates
    MDM substitutions, or validates payload-specific settings.
    """
    if target not in ("generic", "intune", "jamf"):
        raise ValueError("Unknown profile target: {!r}".format(target))
    nodes = _nodes(document)
    issues = []

    def issue(severity, message, path="$", fallback="$"):
        node = nodes.get(path, nodes.get(fallback, document.root))
        issues.append(ProfileIssue(severity, message, path, node.start))

    if target == "intune":
        size = len(xml_bytes(document.source))
        if size > INTUNE_MAX_BYTES:
            issue("error", "Intune custom-profile XML is {} UTF-8 bytes; the upload limit is 1,000,000 bytes.".format(size))
        issue("info", "Intune checks cover common structure and XML upload size. Platform support, assignment, scope and MDM deployment channel require separate verification.")
    elif target == "jamf":
        issue("info", "Jamf import and editing behavior can differ for signed and unsigned profiles and may change settings Jamf does not recognize. Verify the uploaded and deployed profile.")

    root = document.value
    if not isinstance(root, dict):
        issue("error", "A configuration profile must have a dictionary root.")
        return issues

    seen = {"PayloadIdentifier": {}, "PayloadUUID": {}}

    def common(payload, path, top=False):
        for key in ("PayloadType", "PayloadIdentifier", "PayloadUUID"):
            value = payload.get(key)
            key_path = _key_path(path, key)
            if not isinstance(value, str) or not value.strip():
                issue("error", "{} is required and must be a nonempty string.".format(key), key_path, path)
                continue
            if key in seen:
                identity = _uuid_identity(value) if key == "PayloadUUID" else value
                prior = seen[key].get(identity)
                if prior is not None:
                    issue("error", "Duplicate {}; first used at {}.".format(key, prior), key_path, path)
                else:
                    seen[key][identity] = key_path
        if top and payload.get("PayloadType") != "Configuration":
            issue("error", "The root PayloadType must be Configuration.", _key_path(path, "PayloadType"), path)
        version = payload.get("PayloadVersion")
        if type(version) is not int or version != 1:
            issue("error", "PayloadVersion is required and must be the integer 1.", _key_path(path, "PayloadVersion"), path)
        for key in ("PayloadDisplayName", "PayloadDescription", "PayloadOrganization"):
            if key in payload and not isinstance(payload[key], str):
                issue("error", "{} must be a string when present.".format(key), _key_path(path, key), path)
        if "PayloadRemovalDisallowed" in payload and type(payload["PayloadRemovalDisallowed"]) is not bool:
            issue("error", "PayloadRemovalDisallowed must be a boolean when present.", _key_path(path, "PayloadRemovalDisallowed"), path)

    common(root, "$", top=True)
    if "PayloadScope" in root and root["PayloadScope"] not in ("System", "User"):
        issue("error", "PayloadScope must be the string System or User when present.", '$["PayloadScope"]')
    encrypted = "EncryptedPayloadContent" in root
    if encrypted:
        if not isinstance(root["EncryptedPayloadContent"], bytes):
            issue("error", "EncryptedPayloadContent must contain PLIST data.", '$["EncryptedPayloadContent"]')
        else:
            issue("info", "Encrypted payload content is opaque; its payload structure and deployment compatibility cannot be checked.", '$["EncryptedPayloadContent"]')
        if "PayloadContent" in root:
            issue("error", "Use either PayloadContent or EncryptedPayloadContent at the profile root, not both.", '$["PayloadContent"]')
    if "PayloadContent" not in root and encrypted:
        return issues
    content = root.get("PayloadContent")
    if not isinstance(content, list):
        issue("error", "PayloadContent is required and must be an array of payload dictionaries.", '$["PayloadContent"]')
    elif not content:
        issue("warning", "PayloadContent is empty; this is a scaffold with no configuration payloads.", '$["PayloadContent"]')
    else:
        for index, payload in enumerate(content):
            path = '$["PayloadContent"][{}]'.format(index)
            if not isinstance(payload, dict):
                issue("error", "Each PayloadContent item must be a payload dictionary.", path)
            else:
                common(payload, path)
    return issues


def make_profile(display_name="New Configuration Profile", identifier=None,
                 organization="", scope="System"):
    """Return a new XML scaffold with a fresh UUID; never write to disk."""
    if not isinstance(display_name, str) or not isinstance(organization, str):
        raise ValueError("Display name and organization must be strings.")
    if identifier is not None and (not isinstance(identifier, str) or not identifier.strip()):
        raise ValueError("Identifier must be a nonempty string.")
    if scope not in ("System", "User"):
        raise ValueError("Scope must be System or User.")
    profile_uuid = str(uuid.uuid4()).upper()
    profile = {
        "PayloadType": "Configuration", "PayloadVersion": 1,
        "PayloadIdentifier": identifier if identifier is not None else "com.example.profile." + profile_uuid.lower(),
        "PayloadUUID": profile_uuid, "PayloadDisplayName": display_name,
        "PayloadScope": scope, "PayloadContent": [],
    }
    if organization:
        profile["PayloadOrganization"] = organization
    return plistlib.dumps(profile, fmt=plistlib.FMT_XML, sort_keys=False).decode("utf-8")


def append_payload(source, payload_type, display_name="New Payload"):
    """Insert common metadata only, preserving all existing source and identities."""
    try:
        from .plist_core import parse_xml
    except ImportError:
        from plist_core import parse_xml
    if not isinstance(payload_type, str) or not payload_type.strip() or payload_type == "Configuration":
        raise ValueError("Enter a payload domain such as com.apple.wifi.managed or your app's preference domain.")
    if not isinstance(display_name, str):
        raise ValueError("Payload display name must be a string.")
    document = parse_xml(source)
    if not is_profile(document) or "EncryptedPayloadContent" in document.value:
        raise ValueError("Add payloads to an unsigned profile with a plaintext PayloadContent array.")
    identifier = document.value.get("PayloadIdentifier")
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError("Set a nonempty profile PayloadIdentifier before adding a payload.")
    node = _nodes(document).get('$["PayloadContent"]')
    if node is None or node.tag != "array":
        raise ValueError("The profile needs a PayloadContent array before adding a payload.")
    line = source[source.rfind("\n", 0, node.start) + 1:node.start]
    base = re.match(r"[ \t]*", line).group()
    prefix = base + "\t"
    payload_uuid = str(uuid.uuid4()).upper()
    fields = [("PayloadType", payload_type), ("PayloadVersion", 1),
              ("PayloadIdentifier", identifier + "." + payload_uuid.lower()),
              ("PayloadUUID", payload_uuid), ("PayloadDisplayName", display_name)]
    lines = [prefix + "<dict>"]
    for key, value in fields:
        lines.append(prefix + "\t<key>" + key + "</key>")
        tag = "integer" if type(value) is int else "string"
        lines.append(prefix + "\t<" + tag + ">" + html.escape(str(value), quote=False) + "</" + tag + ">")
    lines.append(prefix + "</dict>")
    body = "\n".join(lines)
    if source[node.start:node.open_end].rstrip().endswith("/>"):
        result = source[:node.start] + "<array>\n" + body + "\n" + base + "</array>" + source[node.end:]
    else:
        start = source.rfind("\n", 0, node.close_start) + 1
        if source[start:node.close_start].strip():
            start = node.close_start
        before = source[:start]
        result = before + ("" if before.endswith("\n") else "\n") + body + "\n" + base + source[node.close_start:]
    parse_xml(result)
    return result
