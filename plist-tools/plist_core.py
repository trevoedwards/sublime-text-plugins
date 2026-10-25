"""Strict, source-aware plist parsing. No Sublime dependency or JSON conversion."""
import base64
import bisect
import datetime
import json
import math
import plistlib
import re
import struct
from dataclasses import dataclass, field
from xml.parsers import expat


MAX_DEPTH = 128
MAX_NODES = 100000
VALUE_TAGS = {"dict", "array", "string", "integer", "real", "data", "date", "true", "false"}
CONTAINERS = {"plist", "dict", "array"}


class PlistError(ValueError):
    def __init__(self, message, offset=0):
        super().__init__(message)
        self.offset = offset


@dataclass
class Node:
    tag: str
    start: int
    open_end: int
    end: int = 0
    close_start: int = 0
    text: str = ""
    children: list = field(default_factory=list)
    path: str = "$"
    value: object = None


@dataclass
class Document:
    source: str
    root: Node
    value: object
    entries: list

    def node_at(self, point):
        candidates = [n for n in self.entries if n.start <= point < n.end]
        return min(candidates, key=lambda n: n.end - n.start) if candidates else self.root


def _tag_end(source, start):
    # Attribute values may contain '>'. Expat has already checked their syntax.
    quote = None
    for index in range(start, len(source)):
        ch = source[index]
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == ">":
            return index + 1
    return len(source)


def parse_xml(source):
    """Return typed values and character offsets, rejecting all lossy XML cases."""
    if not isinstance(source, str):
        raise TypeError("parse_xml expects Unicode text")
    if source.lstrip().startswith(("{", "(")):
        raise PlistError("Legacy OpenStep/ASCII property lists are unsupported; convert them to PLIST XML first.")
    # Parse Unicode so an XML encoding declaration describes the disk encoding,
    # not the editor's internal UTF-8 representation.
    byte_positions = [0]
    for ch in source:
        byte_positions.append(byte_positions[-1] + len(ch.encode("utf-8")))
    parser = expat.ParserCreate()
    stack, roots, entries = [], [], []

    def point():
        return bisect.bisect_left(byte_positions, parser.CurrentByteIndex)

    def fail(message, node=None):
        raise PlistError(message, node.start if node else point())

    def start(tag, attrs):
        if len(stack) >= MAX_DEPTH:
            fail("PLIST nesting exceeds the supported depth of {}.".format(MAX_DEPTH))
        if len(entries) >= MAX_NODES:
            fail("PLIST contains too many values to analyze safely.")
        if tag not in VALUE_TAGS | {"plist", "key"}:
            fail("Unknown PLIST element <{}>.".format(tag))
        if attrs and not (tag == "plist" and attrs == {"version": "1.0"}):
            fail("Unexpected attributes on <{}>.".format(tag))
        if tag == "plist" and attrs.get("version", "1.0") != "1.0":
            fail("Only PLIST version 1.0 is supported.")
        node = Node(tag, point(), _tag_end(source, point()))
        if stack:
            stack[-1].children.append(node)
        else:
            roots.append(node)
        stack.append(node)
        entries.append(node)

    def end(tag):
        node = stack.pop()
        empty = source[node.start:node.open_end].rstrip().endswith("/>")
        node.close_start = node.open_end if empty else point()
        node.end = node.open_end if empty else _tag_end(source, point())

    def chars(text):
        if stack:
            stack[-1].text += text
        elif text.strip():
            fail("Text outside the <plist> element.")

    def comment(text):
        if stack:
            at = point()
            finish = source.find("-->", at) + 3
            stack[-1].children.append(Node("#comment", at, finish, finish, finish, text))

    def doctype(name, system_id, public_id, internal):
        if internal or name != "plist":
            fail("Internal DTD subsets and entity declarations are not supported.")
        if system_id not in (None, "http://www.apple.com/DTDs/PropertyList-1.0.dtd"):
            fail("Only Apple's standard PLIST doctype is supported.")

    def instruction(target, data):
        fail("Processing instructions are not supported inside PLIST documents.")

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = chars
    parser.CommentHandler = comment
    parser.StartDoctypeDeclHandler = doctype
    parser.ProcessingInstructionHandler = instruction
    parser.ExternalEntityRefHandler = lambda *args: 0
    try:
        parser.Parse(source, True)
    except expat.ExpatError as error:
        offset = bisect.bisect_left(byte_positions, parser.ErrorByteIndex)
        raise PlistError("XML: {}".format(error), offset) from error
    if len(roots) != 1 or roots[0].tag != "plist":
        raise PlistError("Expected one <plist version=\"1.0\"> root.")

    def convert(node, path):
        node.path = path
        children = [c for c in node.children if c.tag != "#comment"]
        if node.tag in CONTAINERS and node.text.strip():
            fail("Unexpected text inside <{}>.".format(node.tag), node)
        if node.tag not in CONTAINERS and children:
            fail("<{}> cannot contain child elements.".format(node.tag), children[0])
        text = node.text
        if node.tag == "plist":
            if len(children) != 1 or children[0].tag not in VALUE_TAGS:
                fail("<plist> must contain exactly one value.", node)
            result = convert(children[0], path)
        elif node.tag == "dict":
            result = {}
            if len(children) % 2:
                fail("Dictionary key is missing its value.", children[-1])
            for index in range(0, len(children), 2):
                key, val = children[index:index + 2]
                if key.tag != "key":
                    fail("Expected <key> before a dictionary value.", key)
                if val.tag not in VALUE_TAGS:
                    fail("Expected a value after <key>.", val)
                name = convert(key, path)
                key.path = path + "[" + json.dumps(name, ensure_ascii=False) + "]"
                if name in result:
                    fail("Duplicate dictionary key: {!r}.".format(name), key)
                result[name] = convert(val, key.path)
        elif node.tag == "array":
            result = []
            for index, child in enumerate(children):
                if child.tag not in VALUE_TAGS:
                    fail("Arrays contain values, not <key> elements.", child)
                result.append(convert(child, path + "[{}]".format(index)))
        elif node.tag in ("string", "key"):
            result = text
        elif node.tag in ("true", "false"):
            if text.strip():
                fail("Boolean elements must be empty.", node)
            result = node.tag == "true"
        elif node.tag == "integer":
            stripped = text.strip()
            if not re.fullmatch(r"[+-]?(?:[0-9]+|0[xX][0-9a-fA-F]+)", stripped):
                fail("Invalid integer; expected decimal or hexadecimal digits.", node)
            try:
                result = int(stripped, 16 if "x" in stripped.lower() else 10)
            except ValueError:
                fail("Integer is too large to parse safely.", node)
            if not -(2 ** 63) <= result < 2 ** 64:
                fail("Integer is outside the supported PLIST range (-2^63 to 2^64-1).", node)
        elif node.tag == "real":
            stripped = text.strip()
            if not re.fullmatch(r"[+-]?(?:(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?|inf(?:inity)?|nan)", stripped, re.I):
                fail("Invalid real number.", node)
            result = float(stripped)
            if math.isinf(result) and "inf" not in stripped.lower():
                fail("Real number overflows; use 'inf' explicitly if intended.", node)
        elif node.tag == "date":
            if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", text.strip()):
                fail("Dates must use UTC YYYY-MM-DDTHH:MM:SSZ with whole seconds.", node)
            try:
                result = datetime.datetime.strptime(text.strip(), "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                fail("Invalid calendar date or time.", node)
        elif node.tag == "data":
            compact = re.sub(r"[ \t\r\n]", "", text)
            try:
                result = base64.b64decode(compact, validate=True)
                if base64.b64encode(result).decode("ascii") != compact:
                    fail("Data must use canonical base64 padding.", node)
            except (ValueError, UnicodeEncodeError):
                fail("Invalid base64 data.", node)
        node.value = result
        return result

    value = convert(roots[0], "$")
    return Document(source, roots[0], value, entries)


def format_xml(source, indent="\t"):
    """Change structural whitespace only. Keep comments, order and scalar spelling."""
    document = parse_xml(source)

    def render(node, level):
        prefix = indent * level
        if node.tag not in CONTAINERS or not node.children:
            return prefix + source[node.start:node.end]
        opening = source[node.start:node.open_end]
        closing = source[node.close_start:node.end]
        return prefix + opening + "\n" + "\n".join(
            render(child, level + 1) for child in node.children
        ) + "\n" + prefix + closing

    root = document.root
    before = source[:root.start].rstrip()
    after = source[root.end:].strip()
    return (before + "\n" if before else "") + render(root, 0) + ("\n" + after if after else "") + "\n"


def load_binary(data):
    """Reject binary structures whose XML representation would change meaning."""
    class UniqueDict(dict):
        def __setitem__(self, key, value):
            if key in self:
                raise PlistError("Duplicate key in binary dictionary: {!r}.".format(key))
            super().__setitem__(key, value)

    try:
        value = plistlib.loads(data, fmt=plistlib.FMT_BINARY, dict_type=UniqueDict)
    except Exception as error:
        raise PlistError("Invalid binary PLIST: {}".format(error)) from error

    # plistlib rounds binary date doubles to microseconds while loading. Inspect
    # date tokens too, so sub-microsecond fractions cannot silently disappear.
    try:
        offset_size, ref_size, count, root, table = struct.unpack(">6xBBQQQ", data[-32:])
        for index in range(count):
            offset = int.from_bytes(data[table + index * offset_size:table + (index + 1) * offset_size], "big")
            if data[offset] == 0x33:
                seconds = struct.unpack(">d", data[offset + 1:offset + 9])[0]
                if not math.isfinite(seconds) or not seconds.is_integer():
                    raise PlistError("Binary dates with fractional seconds cannot round-trip through PLIST XML; this file is read-only.")
    except (ValueError, IndexError, struct.error) as error:
        raise PlistError("Unsupported binary date or object table: {}".format(error)) from error

    visited = set()

    def check(item, ancestors, depth):
        if depth > MAX_DEPTH:
            raise PlistError("Binary PLIST exceeds the supported nesting depth.")
        if isinstance(item, plistlib.UID):
            raise PlistError("Binary archive UID values cannot be represented safely in XML; this file is read-only.")
        if isinstance(item, (dict, list)):
            if id(item) in ancestors:
                raise PlistError("Cyclic binary PLIST values cannot be represented in XML; this file is read-only.")
            if id(item) in visited:
                raise PlistError("Shared binary container references cannot retain their identity in XML; this file is read-only.")
            visited.add(id(item))
            if len(visited) > MAX_NODES:
                raise PlistError("Binary PLIST contains too many containers to analyze safely.")
            ancestors = ancestors | {id(item)}
            iterable = list(item.keys()) + list(item.values()) if isinstance(item, dict) else item
            for child in iterable:
                check(child, ancestors, depth + 1)
        elif isinstance(item, datetime.datetime) and item.microsecond:
            raise PlistError("Binary dates with fractional seconds cannot round-trip through PLIST XML; this file is read-only.")
        elif item is None:
            raise PlistError("Binary null values have no PLIST XML representation; this file is read-only.")
        elif isinstance(item, str) and "\r" in item:
            raise PlistError("Binary strings containing carriage returns cannot round-trip through plistlib XML; this file is read-only.")

    check(value, set(), 0)
    try:
        xml = plistlib.dumps(value, fmt=plistlib.FMT_XML, sort_keys=False).decode("utf-8")
        if not semantic_equal(value, parse_xml(xml).value):
            raise PlistError("Binary values cannot round-trip through XML without changing their types, order or floating-point bits.")
    except (ValueError, TypeError, OverflowError) as error:
        raise PlistError("Binary values cannot safely round-trip through XML: {}".format(error)) from error
    return xml


def binary_from_xml(source):
    value = parse_xml(source).value
    result = plistlib.dumps(value, fmt=plistlib.FMT_BINARY, sort_keys=False)
    if not semantic_equal(value, plistlib.loads(result, fmt=plistlib.FMT_BINARY)):
        raise PlistError("Binary serialization would change a value (for example mixed positive and negative zero). Export XML or adjust those values; no binary file was written.")
    return result


def semantic_equal(left, right):
    """Ordered, type-strict equality, including signed zero and NaN payloads."""
    if isinstance(left, dict) and isinstance(right, dict):
        return (list(left) == list(right) and all(semantic_equal(left[k], right[k]) for k in left))
    if type(left) is not type(right):
        return False
    if isinstance(left, list):
        return len(left) == len(right) and all(semantic_equal(a, b) for a, b in zip(left, right))
    if isinstance(left, float):
        return struct.pack(">d", left) == struct.pack(">d", right)
    return left == right


def xml_bytes(source):
    """Export UTF-8 XML with a matching declaration, preserving the document body."""
    parse_xml(source)
    source = re.sub(r"(<\?xml\b[^?]*encoding\s*=\s*)['\"][^'\"]+['\"]", r'\1"UTF-8"', source, count=1)
    return source.encode("utf-8")
