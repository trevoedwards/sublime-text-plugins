import copy
import os
import plistlib
import sys
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from plist_core import parse_xml, xml_bytes
from mobileconfig import analyze_profile, append_payload, is_profile, make_profile, profile_payloads


def document(value):
    return parse_xml(plistlib.dumps(value, sort_keys=False).decode("utf-8"))


def payload(payload_type="com.example.app", suffix="one", **settings):
    result = {"PayloadType": payload_type, "PayloadIdentifier": "com.example." + suffix,
              "PayloadUUID": str(uuid.uuid4()), "PayloadVersion": 1}
    result.update(settings)
    return result


def profile(*children):
    result = plistlib.loads(make_profile())
    result["PayloadContent"] = list(children)
    return result


def errors(doc, target="generic"):
    return [item for item in analyze_profile(doc, target) if item.severity == "error"]


class ProfileTests(unittest.TestCase):
    def test_appending_payloads_preserves_existing_source_and_identities(self):
        source = make_profile("Existing\nProfile", "com.example.company")
        source = source.replace("<array/>", "<array><!-- keep me --></array>")
        source = source.replace("<key>PayloadType</key>", "<!-- retained comment -->\n<key>PayloadType</key>", 1)
        before = parse_xml(source).value
        updated = append_payload(source, "com.apple.TCC.configuration-profile-policy", "Privacy & Control\nSecond line")
        after = parse_xml(updated).value
        self.assertIn("<!-- keep me -->", updated)
        self.assertIn("<!-- retained comment -->", updated)
        for key in before:
            if key != "PayloadContent":
                self.assertEqual(before[key], after[key])
        self.assertEqual("Privacy & Control\nSecond line", after["PayloadContent"][0]["PayloadDisplayName"])
        twice = parse_xml(append_payload(updated, "com.example.custom.application")).value
        self.assertEqual(after["PayloadContent"][0], twice["PayloadContent"][0])
        self.assertNotEqual(twice["PayloadContent"][0]["PayloadUUID"], twice["PayloadContent"][1]["PayloadUUID"])
        self.assertEqual([], errors(parse_xml(updated)))

    def test_appending_to_empty_self_closing_array_and_rejecting_opaque(self):
        source = make_profile()
        self.assertEqual(1, len(parse_xml(append_payload(source, "com.apple.wifi.managed")).value["PayloadContent"]))
        for bad in ("", "  ", "Configuration", None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                append_payload(source, bad)
        opaque = profile()
        del opaque["PayloadContent"]
        opaque["EncryptedPayloadContent"] = b"opaque"
        with self.assertRaises(ValueError):
            append_payload(document(opaque).source, "com.example.app")

    def test_jamf_pppc_system_extensions_and_app_domains_are_accepted(self):
        value = profile(
            payload("com.apple.TCC.configuration-profile-policy", "pppc", Services={
                "SystemPolicyAllFiles": [{"Identifier": "com.example.agent", "Allowed": True}]}),
            payload("com.apple.system-extension-policy", "extension", AllowedSystemExtensions={
                "TEAM123": ["com.example.extension"]}),
            payload("com.acme.custom.application", "app", FutureVendorSetting={"AnyKey": [1, True]}),
        )
        doc = document(value)
        self.assertTrue(is_profile(doc))
        self.assertEqual([], analyze_profile(doc))
        self.assertEqual([], errors(doc, "jamf"))
        self.assertEqual(value, doc.value)
        self.assertTrue(any("Jamf" in item.message for item in analyze_profile(doc, "jamf")))

    def test_intune_tokens_are_preserved_without_uuid_format_rejection(self):
        value = profile(payload("com.apple.wifi.managed", "wifi", SSID_STR="Corporate",
                                UserName="{{username}}", Password="{{serialnumber}}"))
        value["PayloadIdentifier"] = "com.example.$SERIALNUMBER"
        value["PayloadUUID"] = "$SERIALNUMBER-$USERNAME"
        value["PayloadContent"][0]["PayloadUUID"] = "{{serialnumber}}"
        doc = document(value)
        before = doc.source
        self.assertEqual([], errors(doc, "intune"))
        self.assertEqual([], analyze_profile(doc))
        self.assertEqual(before, doc.source)
        self.assertEqual(value, doc.value)

    def test_nonprofile_root_and_wrong_payload_type(self):
        for value in ([], "profile", True, 1):
            with self.subTest(value=value):
                doc = document(value)
                self.assertFalse(is_profile(doc))
                self.assertTrue(any("dictionary root" in item.message for item in errors(doc)))
        value = profile(payload())
        value["PayloadType"] = "com.example.application"
        doc = document(value)
        self.assertFalse(is_profile(doc))
        self.assertTrue(any("root PayloadType" in item.message for item in errors(doc)))

    def test_required_common_keys_in_root_and_children(self):
        for where in ("root", "child"):
            for key in ("PayloadIdentifier", "PayloadUUID", "PayloadType", "PayloadVersion"):
                value = profile(payload())
                target = value if where == "root" else value["PayloadContent"][0]
                del target[key]
                with self.subTest(where=where, key=key):
                    issues = errors(document(value))
                    expected = '$["{}"]'.format(key) if where == "root" else '$["PayloadContent"][0]["{}"]'.format(key)
                    self.assertTrue(any(item.path == expected for item in issues))

    def test_wrong_common_types_and_versions_in_root_and_child(self):
        invalid = {"PayloadIdentifier": [""], "PayloadUUID": b"x", "PayloadType": 1,
                   "PayloadDisplayName": {}, "PayloadDescription": False,
                   "PayloadOrganization": 9, "PayloadRemovalDisallowed": 1}
        for where in ("root", "child"):
            for key, bad in invalid.items():
                value = profile(payload())
                target = value if where == "root" else value["PayloadContent"][0]
                target[key] = bad
                with self.subTest(where=where, key=key):
                    self.assertTrue(any(key in item.message for item in errors(document(value))))
            for bad in (True, False, 1.0, "1", 0, 2):
                value = profile(payload())
                target = value if where == "root" else value["PayloadContent"][0]
                target["PayloadVersion"] = bad
                with self.subTest(where=where, version=bad):
                    self.assertTrue(any("PayloadVersion" in item.message for item in errors(document(value))))
        for key in ("PayloadIdentifier", "PayloadUUID", "PayloadType"):
            value = profile(payload())
            value["PayloadContent"][0][key] = "   "
            self.assertTrue(any(key in item.message for item in errors(document(value))))

    def test_content_and_scope_structure(self):
        value = profile()
        del value["PayloadContent"]
        self.assertTrue(errors(document(value)))
        for content in ({}, "payloads", [False], ["nested"]):
            value["PayloadContent"] = content
            with self.subTest(content=content):
                self.assertTrue(errors(document(value)))
        value = profile(payload())
        for scope in ("System", "User"):
            value["PayloadScope"] = scope
            self.assertEqual([], errors(document(value)))
        for scope in ("Device", True, 1, []):
            value["PayloadScope"] = scope
            self.assertTrue(any("PayloadScope" in item.message for item in errors(document(value))))

    def test_duplicate_identifiers_and_case_insensitive_uuid_duplicates(self):
        first = payload(suffix="first")
        second = payload(suffix="second")
        value = profile(first, second)
        second["PayloadIdentifier"] = first["PayloadIdentifier"]
        second["PayloadUUID"] = first["PayloadUUID"].upper()
        doc = document(value)
        issues = [item for item in errors(doc) if "Duplicate" in item.message]
        self.assertEqual(2, len(issues))
        for issue in issues:
            self.assertIn('$["PayloadContent"][1]', issue.path)
            self.assertIn('first used at $["PayloadContent"][0]', issue.message)
            self.assertEqual(issue.path, doc.node_at(issue.offset).path)
        value = profile(first)
        value["PayloadUUID"] = first["PayloadUUID"]
        value["PayloadIdentifier"] = first["PayloadIdentifier"]
        self.assertEqual(2, len([item for item in errors(document(value)) if "Duplicate" in item.message]))

    def test_offsets_are_character_offsets_after_unicode(self):
        value = profile(payload(PayloadDisplayName="café 🚀"), payload(suffix="second"))
        value["PayloadContent"][1]["PayloadUUID"] = value["PayloadContent"][0]["PayloadUUID"]
        doc = document(value)
        duplicate = next(item for item in errors(doc) if "Duplicate" in item.message)
        text = '<string>' + value["PayloadContent"][0]["PayloadUUID"] + '</string>'
        self.assertEqual(doc.source.rindex(text), duplicate.offset)
        self.assertNotEqual(len(doc.source[:duplicate.offset].encode("utf-8")), duplicate.offset)
        del value["PayloadContent"][1]["PayloadIdentifier"]
        doc = document(value)
        missing = next(item for item in errors(doc) if "PayloadIdentifier is required" in item.message)
        self.assertEqual('$["PayloadContent"][1]', doc.node_at(missing.offset).path)

    def test_payload_outline_is_immediate_and_does_not_dump_settings(self):
        value = profile(payload(PayloadDisplayName="Wi-Fi", Password="TOP SECRET",
                                Nested={"PayloadType": "fake", "PayloadContent": [{}]}), False)
        doc = document(value)
        outline = profile_payloads(doc)
        self.assertEqual(1, len(outline))
        self.assertEqual('$["PayloadContent"][0]', outline[0].path)
        self.assertEqual("Wi-Fi", outline[0].display_name)
        self.assertEqual("dict", doc.node_at(outline[0].offset).tag)
        self.assertNotIn("TOP SECRET", repr(outline))
        self.assertNotIn("Nested", repr(outline))
        value["PayloadContent"][0]["PayloadDisplayName"] = {"Password": "TOP SECRET"}
        value["PayloadContent"][0]["PayloadVersion"] = True
        outline = profile_payloads(document(value))
        self.assertIsNone(outline[0].display_name)
        self.assertIsNone(outline[0].version)
        self.assertEqual([], profile_payloads(document([])))

    def test_encrypted_content_is_opaque_not_a_missing_payload_error(self):
        value = profile()
        del value["PayloadContent"]
        value["EncryptedPayloadContent"] = b"opaque encrypted content"
        doc = document(value)
        self.assertEqual([], errors(doc))
        self.assertEqual([], profile_payloads(doc))
        self.assertTrue(any("opaque" in item.message for item in analyze_profile(doc)))
        value["EncryptedPayloadContent"] = "not data"
        self.assertTrue(any("PLIST data" in item.message for item in errors(document(value))))
        value["EncryptedPayloadContent"] = b"encrypted"
        value["PayloadContent"] = []
        self.assertTrue(any("not both" in item.message for item in errors(document(value))))

    def test_new_profiles_have_unique_ids_and_valid_scaffolds(self):
        one = parse_xml(make_profile())
        two = parse_xml(make_profile("Corporate Wi-Fi", "com.example.wifi", "Example", "User"))
        self.assertNotEqual(one.value["PayloadUUID"], two.value["PayloadUUID"])
        uuid.UUID(one.value["PayloadUUID"])
        self.assertTrue(is_profile(one))
        self.assertEqual("com.example.wifi", two.value["PayloadIdentifier"])
        self.assertEqual("Example", two.value["PayloadOrganization"])
        self.assertEqual("User", two.value["PayloadScope"])
        self.assertEqual([], errors(one))
        self.assertEqual(["warning"], [item.severity for item in analyze_profile(one)])
        third = parse_xml(make_profile())
        self.assertNotEqual(one.value["PayloadIdentifier"], third.value["PayloadIdentifier"])

    def test_intune_limit_uses_exact_utf8_xml_export_bytes(self):
        # UTF-16 -> UTF-8 declaration normalization changes the export length.
        doc = document(profile(payload(PayloadDisplayName="café 🚀")))
        source = doc.source.replace('encoding="UTF-8"', 'encoding="UTF-16"')
        prefix = '<!--'
        suffix = '-->\n'
        padding = 1000000 - len(xml_bytes(source)) - len(prefix + suffix)
        source += prefix + "x" * padding + suffix
        exact = parse_xml(source)
        self.assertEqual(1000000, len(xml_bytes(exact.source)))
        self.assertEqual([], errors(exact, "intune"))
        over = parse_xml(source + " ")
        self.assertTrue(any("1,000,000" in item.message for item in errors(over, "intune")))
        self.assertEqual([], errors(over))

    def test_analysis_does_not_mutate_values_or_source(self):
        doc = document(profile(payload(Unknown={"Array": [True, b"private", 7]})))
        original = copy.deepcopy(doc.value)
        source = doc.source
        for target in ("generic", "jamf", "intune"):
            analyze_profile(doc, target)
            profile_payloads(doc)
        self.assertEqual(original, doc.value)
        self.assertEqual(source, doc.source)


if __name__ == "__main__":
    unittest.main()
