import datetime
import math
import os
import plistlib
import sys
import struct
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from plist_core import PlistError, binary_from_xml, format_xml, load_binary, parse_xml, xml_bytes


def wrap(body):
    return '<plist version="1.0">' + body + '</plist>'


class ParserTests(unittest.TestCase):
    def test_all_types_and_order_round_trip(self):
        values = {"z": [True, False, 7, -9, 2.5, b"\x00\xff", "café 🚀"],
                  "a": datetime.datetime(2020, 2, 29, 12, 34, 56), "empty": {}}
        xml = plistlib.dumps(values, sort_keys=False).decode()
        parsed = parse_xml(xml).value
        self.assertEqual(values, parsed)
        binary = binary_from_xml(xml)
        self.assertTrue(binary.startswith(b"bplist00"))
        self.assertEqual(values, plistlib.loads(binary))
        self.assertEqual(list(parse_xml(load_binary(binary)).value), list(values))
        self.assertIs(type(parsed["z"][0]), bool)
        self.assertIs(type(parsed["z"][2]), int)
        self.assertIs(type(parsed["z"][4]), float)
        self.assertIs(type(parsed["z"][5]), bytes)

    def test_strict_structure(self):
        invalid = ["<wat/>", "<dict><string>x</string><integer>1</integer></dict>",
                   "<dict><key>x</key></dict>", "<array><key>x</key></array>",
                   "<string><true/></string>", "<true>yes</true>",
                   "<dict>garbage</dict>", "<dict><key>x</key><true/><key>x</key><false/></dict>",
                   "<key>x</key>", "<true/><false/>", "<integer unit='n'>1</integer>"]
        for body in invalid:
            with self.subTest(body=body), self.assertRaises(PlistError):
                parse_xml(wrap(body))

    def test_strict_scalar_validation(self):
        invalid = ["<integer>1.2</integer>", "<integer>10_000</integer>",
                   "<integer>{}</integer>".format(2 ** 64),
                   "<integer>{}</integer>".format(-(2 ** 63) - 1),
                   "<real>1e9999</real>", "<real>one</real>", "<data>!@#</data>",
                   "<data>AB==</data>", "<data>AA</data>",
                   "<date>2020-02-30T00:00:00Z</date>",
                   "<date>2020-02-01T00:00:00+00:00</date>",
                   "<date>2020-02-01T00:00:00.5Z</date>"]
        for body in invalid:
            with self.subTest(body=body), self.assertRaises(PlistError):
                parse_xml(wrap(body))
        self.assertEqual(255, parse_xml(wrap("<integer>0xff</integer>")).value)
        self.assertEqual(b"\0", parse_xml(wrap("<data> AA==\n</data>")).value)
        self.assertTrue(math.isnan(parse_xml(wrap("<real>nan</real>")).value))
        self.assertEqual(float("inf"), parse_xml(wrap("<real>inf</real>")).value)
        self.assertEqual(float("inf"), parse_xml(wrap("<real>+infinity</real>")).value)
        self.assertEqual(float("-inf"), parse_xml(wrap("<real>-infinity</real>")).value)

    def test_unicode_offsets_key_paths_and_boundaries(self):
        source = wrap('<dict><key>café🚀</key><array><string>élan</string><true/></array></dict>')
        document = parse_xml(source)
        for node in document.entries:
            self.assertTrue(source[node.start:node.end].startswith("<" + node.tag))
            self.assertTrue(source[node.start:node.end].endswith(">"))
        pos = source.index("<true")
        self.assertEqual('$["café🚀"][1]', document.node_at(pos).path)
        self.assertEqual("true", document.node_at(pos).tag)
        duplicate = wrap('<dict><key>🚀</key><true/><key>🚀</key><false/></dict>')
        with self.assertRaises(PlistError) as raised:
            parse_xml(duplicate)
        self.assertEqual(duplicate.rindex("<key>"), raised.exception.offset)

    def test_entity_comments_and_whitespace(self):
        text = parse_xml(wrap('<string> a &amp; b <!--note-->\n c </string>')).value
        self.assertEqual(" a & b \n c ", text)
        with self.assertRaises(PlistError):
            parse_xml('<!DOCTYPE plist [<!ENTITY x "hello">]>' + wrap('<string>&x;</string>'))

    def test_format_preserves_scalar_spelling_comments_and_order(self):
        source = '<?xml version="1.0"?>\n<!-- header -->\n' + wrap(
            '<dict> <key>z</key><!-- comment --><string>  keep\n  this &amp; that </string>'
            '<key>a</key><integer>0x0f</integer></dict>') + '<!-- footer -->'
        result = format_xml(source, "  ")
        self.assertEqual(parse_xml(source).value, parse_xml(result).value)
        self.assertIn("<!-- header -->", result)
        self.assertIn("<!-- comment -->", result)
        self.assertIn("<!-- footer -->", result)
        self.assertIn("<integer>0x0f</integer>", result)
        self.assertIn("<string>  keep\n  this &amp; that </string>", result)
        self.assertEqual(result, format_xml(result, "  "))

    def test_utf16_decl_in_unicode_editor_text(self):
        source = '<?xml version="1.0" encoding="UTF-16"?>' + wrap('<string>é</string>')
        self.assertEqual("é", parse_xml(source).value)
        self.assertIn(b'encoding="UTF-8"', xml_bytes(source))

    def test_binary_rejects_nonrepresentable_values(self):
        cycle = []
        cycle.append(cycle)
        shared = {"child": 7}
        for value in [plistlib.UID(1), None, cycle, [shared, shared], datetime.datetime(2020, 1, 1, microsecond=123), "\x01"]:
            with self.subTest(value=type(value)), self.assertRaises(PlistError):
                load_binary(plistlib.dumps(value, fmt=plistlib.FMT_BINARY))

    def test_openstep_has_specific_error(self):
        with self.assertRaisesRegex(PlistError, "OpenStep"):
            parse_xml('{ Name = "value"; }')

    def test_binary_infinity_nan_preserved(self):
        for value in [float("inf"), float("-inf"), float("nan")]:
            result = parse_xml(load_binary(plistlib.dumps(value, fmt=plistlib.FMT_BINARY))).value
            self.assertTrue(math.isnan(result) if math.isnan(value) else result == value)

    def test_carriage_returns_are_rejected_instead_of_normalized(self):
        data = plistlib.dumps({"text": "a\r\nb\rc"}, fmt=plistlib.FMT_BINARY)
        with self.assertRaisesRegex(PlistError, "carriage returns"):
            load_binary(data)

    def test_mixed_signed_zero_binary_serialization_is_rejected(self):
        xml = wrap('<array><real>0.0</real><real>-0.0</real></array>')
        with self.assertRaisesRegex(PlistError, "positive and negative zero"):
            binary_from_xml(xml)
        single = binary_from_xml(wrap('<real>-0.0</real>'))
        self.assertEqual(struct.pack('>d', -0.0), struct.pack('>d', plistlib.loads(single)))

    def test_huge_integer_error_points_to_value(self):
        xml = wrap('<array><integer>' + '9' * 10000 + '</integer></array>')
        with self.assertRaises(PlistError) as raised:
            parse_xml(xml)
        self.assertEqual(xml.index('<integer>'), raised.exception.offset)

    def test_depth_bound(self):
        with self.assertRaises(PlistError):
            parse_xml(wrap("<array>" * 150 + "<true/>" + "</array>" * 150))


if __name__ == "__main__":
    unittest.main()
