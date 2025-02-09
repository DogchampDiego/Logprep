# pylint: disable=missing-docstring
# pylint: disable=protected-access
# pylint: disable=import-error
import pytest

from tests.unit.processor.base import BaseProcessorTestCase

test_cases = [  # testcase, rule, event, expected
    (
        "writes missing root-key in the missing_fields Field",
        {
            "filter": "*",
            "key_checker": {
                "key_list": ["key2"],
                "output_field": "missing_fields",
            },
        },
        {
            "testkey": "key1_value",
            "_index": "value",
        },
        {
            "testkey": "key1_value",
            "_index": "value",
            "missing_fields": ["key2"],
        },
    ),
    (
        "writes missing sub-key in the missing_fields Field",
        {
            "filter": "*",
            "key_checker": {
                "key_list": ["testkey.key2"],
                "output_field": "missing_fields",
            },
        },
        {"testkey": {"key1": "key1_value", "_index": "value"}},
        {
            "testkey": {
                "key1": "key1_value",
                "_index": "value",
            },
            "missing_fields": ["testkey.key2"],
        },
    ),
    (
        "writes the missing key from a list with one missing and 3 existing keys in the missing_fields Field",
        {
            "filter": "*",
            "key_checker": {
                "key_list": ["key1.key2", "key1", "key1.key2.key3", "key4"],
                "output_field": "missing_fields",
            },
        },
        {
            "key1": {
                "key2": {"key3": {"key3": "key3_value"}, "random_key": "random_key_value"},
                "_index": "value",
            }
        },
        {
            "key1": {
                "key2": {"key3": {"key3": "key3_value"}, "random_key": "random_key_value"},
                "_index": "value",
            },
            "missing_fields": ["key4"],
        },
    ),
    (
        "Detects 'root-key1' in the event",
        {
            "filter": "*",
            "key_checker": {
                "key_list": ["key1"],
                "output_field": "missing_fields",
            },
        },
        {
            "key1": {
                "key2": {"key3": "key3_value", "random_key": "random_key_value"},
                "_index": "value",
            }
        },
        {
            "key1": {
                "key2": {"key3": "key3_value", "random_key": "random_key_value"},
                "_index": "value",
            }
        },
    ),
    (
        "Detects 'sub-key2' in the event",
        {
            "filter": "*",
            "key_checker": {
                "key_list": ["testkey.key2"],
                "output_field": "missing_fields",
            },
        },
        {
            "testkey": {
                "key2": {"key3": "key3_value", "random_key": "random_key_value"},
                "_index": "value",
            }
        },
        {
            "testkey": {
                "key2": {"key3": "key3_value", "random_key": "random_key_value"},
                "_index": "value",
            },
        },
    ),
    (
        "Detects multiple Keys",
        {
            "filter": "*",
            "key_checker": {
                "key_list": ["key1.key2", "key1", "key1.key2.key3"],
                "output_field": "missing_fields",
            },
        },
        {
            "key1": {
                "key2": {"key3": {"key3": "key3_value"}, "random_key": "random_key_value"},
                "_index": "value",
            }
        },
        {
            "key1": {
                "key2": {"key3": {"key3": "key3_value"}, "random_key": "random_key_value"},
                "_index": "value",
            }
        },
    ),
]


class TestKeyChecker(BaseProcessorTestCase):
    timeout = 0.01

    CONFIG = {
        "type": "key_checker",
        "specific_rules": ["tests/testdata/unit/key_checker/"],
        "generic_rules": ["tests/testdata/unit/key_checker/"],
    }

    @property
    def generic_rules_dirs(self):
        return self.CONFIG["generic_rules"]

    @property
    def specific_rules_dirs(self):
        return self.CONFIG["specific_rules"]

    @pytest.mark.parametrize("testcase, rule, event, expected", test_cases)
    def test_testcases_positiv(
        self, testcase, rule, event, expected
    ):  # pylint: disable=unused-argument
        self._load_specific_rule(rule)
        self.object.process(event)
        assert event == expected
