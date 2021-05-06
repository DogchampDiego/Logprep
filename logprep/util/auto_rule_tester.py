#!/usr/bin/python3
"""This module implements an auto-tester that can execute tests for rules."""

from typing import Union, Optional

import inspect
import pathlib
from difflib import ndiff
import tempfile
from yaml import safe_load
from ruamel.yaml import safe_load_all, YAMLError
from os import walk
from pprint import pprint
from collections import defaultdict
import regex as re
from colorama import Fore
import traceback

from logprep.framework.rule_tree.rule_tree import RuleTree
from tests.acceptance.util import *

from logprep.processor.processor_factory import ProcessorFactory
from logprep.processor.base.rule import Rule
from logprep.processor.base.processor import RuleBasedProcessor

from logprep.processor.pre_detector.processor import PreDetector

from logprep.util.grok_pattern_loader import GrokPatternLoader as gpl
from logprep.util.helper import print_fcolor

logger = getLogger()
logger.disabled = True


class AutoRuleTesterException(BaseException):
    """Base class for AutoRuleTester related exceptions."""

    def __init__(self, message: str):
        super().__init__(f'AutoRuleTester ({message}): ')


class GrokPatternReplacer:
    """Used to replace strings with pre-defined grok patterns."""

    def __init__(self, config: dict):
        self._grok_patterns = {
            'PSEUDONYM': r'<pseudonym:[a-fA-F0-9]{64}>',
            'UUID': r'[a-fA-F0-9]{8}-(?:[a-fA-F0-9]{4}-){3}[a-fA-F0-9]{12}',
            'NUMBER': r'[-+]?[0-9]*\.?[0-9]+([eE][-+]?[0-9]+)?',
            'WORD': r'\w+',
            'IPV4': r'\d{1,3}(\.\d{1,3}){3}',
            'IPV4_PORT': r'\d{1,3}(\.\d{1,3}){3}:\d+'
        }

        additional_patterns_list = list()
        pipeline_cfg = config.get('pipeline', list())
        for processor_cfg in pipeline_cfg:
            processor_values = list(processor_cfg.values())[0]
            additional_patterns = processor_values.get('grok_patterns')
            if additional_patterns:
                additional_patterns_list.append(processor_values.get('grok_patterns'))

        for additional_patterns in additional_patterns_list:
            if isinstance(additional_patterns, str):
                additional_patterns = [additional_patterns]
            for auto_test_pattern in additional_patterns:
                self._grok_patterns.update(gpl.load(auto_test_pattern))

        print('\nGrok Patterns:')
        pprint(self._grok_patterns)

        self._grok_base = re.compile(r'%\{.*?\}')

    @staticmethod
    def _change_dotted_field_value(event: dict, dotted_field: str, new_value: str):
        fields = dotted_field.split('.')
        dict_ = event
        last_field = None
        for field in fields:
            if field in dict_ and isinstance(dict_[field], dict):
                dict_ = dict_[field]
            last_field = field
        if last_field:
            dict_[last_field] = new_value

    @staticmethod
    def _get_dotted_field_value(event: dict, dotted_field: str) -> Optional[Union[dict, list, str]]:
        fields = dotted_field.split('.')
        dict_ = event
        for field in fields:
            if field in dict_:
                dict_ = dict_[field]
            else:
                return None
        return dict_

    def _replace_all_keywords_in_value(self, dotted_value: str) -> str:
        while bool(self._grok_base.search(str(dotted_value))):
            for identifier, grok_value in self._grok_patterns.items():
                pattern = '%{' + identifier + '}'
                dotted_value = str(dotted_value)
                dotted_value = dotted_value.replace(pattern, grok_value)
        return dotted_value

    def replace_grok_keywords(self, processed: dict, reference_dict: dict, dotted_field: str = ''):
        """Create aggregating logger.

        Parameters
        ----------
        processed : dict
            Expected test result for rule test.
        reference_dict : dict
            Original test data containing test input and expected.
        dotted_field : str
            Field that contains value that should be replaced.

        """
        for processed_field, processed_sub in list(processed.items()):
            dotted_field_tmp = dotted_field
            dotted_field += '.{}'.format(processed_field) if dotted_field else processed_field
            dotted_value = self._get_dotted_field_value(reference_dict['processed'], dotted_field)

            if isinstance(dotted_value, (str, int, float)):
                if processed_field.endswith('|re'):
                    new_key = processed_field.replace('|re', '')
                    dotted_value_raw = self._get_dotted_field_value(reference_dict['raw'],
                                                                    dotted_field.replace('|re', ''))

                    grok_keywords_in_value = set(self._grok_base.findall(str(dotted_value)))
                    defined_grok_keywords = ['%{' + grok_definition + '}' for grok_definition in
                                             self._grok_patterns.keys()]

                    if all([(grok_keyword in defined_grok_keywords) for grok_keyword in grok_keywords_in_value]):
                        dotted_value = self._replace_all_keywords_in_value(dotted_value)

                    dotted_value = '^' + dotted_value + '$'

                    if dotted_value_raw:
                        grok_keywords_in_value = bool(re.search(dotted_value, str(dotted_value_raw)))
                    else:
                        grok_keywords_in_value = False

                    if grok_keywords_in_value:
                        self._change_dotted_field_value(reference_dict['processed'], dotted_field, dotted_value_raw)
                    else:
                        self._change_dotted_field_value(reference_dict['processed'], dotted_field, dotted_value)

                    processed[new_key] = processed.pop(processed_field)

            # Sort lists to have same ordering in raw and processed for later comparison
            if isinstance(processed_sub, list):
                processed[processed_field] = sorted(processed_sub)

            if isinstance(processed_sub, dict):
                self.replace_grok_keywords(processed_sub, reference_dict, dotted_field=dotted_field)
            dotted_field = dotted_field_tmp


class PreDetectionExtraHandler:
    """Used to handle special demands for PreDetector auto-tests."""

    @staticmethod
    def _get_errors(processor: RuleBasedProcessor, extra_output: tuple):
        pd_errors = []
        pd_warnings = []
        if isinstance(processor, PreDetector):
            if not extra_output:
                return pd_errors, pd_warnings

            pre_detection_extra_out = extra_output[0][0]
            mitre_out = pre_detection_extra_out.get('mitre')
            id_out = pre_detection_extra_out.get('id')

            mitre_pattern = r'^.*\.(t|T)\d{4}(\.\d{3})?$'
            uuid_pattern = r'^[a-fA-F0-9]{8}-(?:[a-fA-F0-9]{4}-){3}[a-fA-F0-9]{12}$'

            if not re.search(uuid_pattern, id_out):
                pd_warnings.append('Warning in extra output: "id: {}" is not a valid UUID!'.format(id_out))

            if 'pre_detection_id' not in pre_detection_extra_out:
                pd_errors.append('Error in extra output: "id" field does not exist!')

            if 'mitre' not in pre_detection_extra_out:
                pd_errors.append('Error in extra output: "mitre" field does not exist!')
            elif not any([technique for technique in mitre_out if re.search(mitre_pattern, technique)]):
                pd_errors.append('Error in extra output: "mitre: {}" does not include a valid '
                                 'mitre attack technique!'.format(mitre_out))
        return pd_errors, pd_warnings

    def update_errors(self, processor: PreDetector, extra_output: tuple, errors: list, warnings: list):
        """Create aggregating logger.

        Parameters
        ----------
        processor : PreDetector
            Processor that should be of type PreDetector.
        extra_output : dict
            Extra output containing MITRE information coming from PreDetector.
        errors : list
            List of errors.
        warnings : list
            List of warnings.

        """
        mitre_errors, id_warnings = self._get_errors(processor, extra_output)
        errors += mitre_errors
        warnings += id_warnings


class AutoRuleTester:
    """Used to perform auto-tests for rules."""

    def __init__(self, config):
        with open(config, 'r') as yaml_file:
            self._config_yml = safe_load(yaml_file)

        self._empty_rules_dirs = [tempfile.mkdtemp()]

        self._config_yml['connector'] = {'type': 'dummy'}
        self._config_yml['process_count'] = 1
        self._config_yml['timeout'] = 0.1

        self._enable_print_stack_trace = self._config_yml.get('print_auto_test_stack_trace', True)

        self._success = True

        self._successful_rule_tests_cnt = 0
        self._failed_rule_tests_cnt = 0
        self._warning_cnt = 0

        self._pd_extra = PreDetectionExtraHandler()

        self._filename_printed = False

        self._gpl = GrokPatternReplacer(self._config_yml)

    def run(self):
        """Perform auto-tests."""
        rules_dirs = self._get_rule_dirs_by_processor_type()
        rules_pp = self._get_rules_per_processor_type(rules_dirs)

        self._check_which_rule_files_miss_tests(rules_pp)
        self._set_rules_dirs_to_empty()

        for processor_in_pipeline in self._config_yml['pipeline']:
            name, processor_cfg = next(iter(processor_in_pipeline.items()))
            processor, rule_class = self._get_processor_instance_and_rule_type(name, processor_cfg, logger)

            for rule_test in rules_pp[processor_cfg['type']]:
                if processor and rule_class:
                    if rule_test['tests']:
                        self._run_rule_tests_from_file(processor, rule_test, rule_class, processor_cfg)

        print_fcolor(Fore.WHITE, '\nResults:')
        print_fcolor(Fore.RED, f'Failed tests: {self._failed_rule_tests_cnt}')
        print_fcolor(Fore.GREEN, f'Successful tests: {self._successful_rule_tests_cnt}')
        print_fcolor(Fore.CYAN, f'Total tests: {self._successful_rule_tests_cnt + self._failed_rule_tests_cnt}')
        print_fcolor(Fore.YELLOW, f'Warnings: {self._warning_cnt}')

        if not self._success:
            exit(1)

    def _run_rule_tests_from_file(self, processor, rule_test, rule_class, processor_cfg):
        temp_rule_path = path.join(self._empty_rules_dirs[0], 'temp.json')
        if processor_cfg.get('tree_config'):
            for idx, rule_dict in enumerate(rule_test.get('rules', [])):
                with open(temp_rule_path, 'w') as temp_file:
                    json.dump([rule_dict], temp_file)
                processor.add_rules_from_directory(self._empty_rules_dirs)
                self._eval_rule_test(rule_test, processor, idx)
                processor._tree = RuleTree()
                remove_file_if_exists(temp_rule_path)
        elif rule_test.get('rules'):
            for idx, rule_dict in enumerate(rule_test.get('rules', [])):
                with open(temp_rule_path, 'w') as temp_file:
                    json.dump([rule_dict], temp_file)
                processor.add_rules_from_directory(self._empty_rules_dirs)
                self._eval_rule_test(rule_test, processor, idx)
                processor._rules.clear()
                remove_file_if_exists(temp_rule_path)
        elif rule_test.get('specific_rules') or rule_test.get('generic_rules'):
            if rule_test.get('specific_rules'):
                for idx, rule_dict in enumerate(rule_test.get('specific_rules', [])):
                    with open(temp_rule_path, 'w') as temp_file:
                        json.dump([rule_dict], temp_file)
                    processor.add_rules_from_directory(self._empty_rules_dirs, [])
                    self._eval_rule_test(rule_test, processor, idx)
                    processor._specific_rules.clear()
                    remove_file_if_exists(temp_rule_path)
            if rule_test.get('generic_rules'):
                for idx, rule_dict in enumerate(rule_test.get('generic_rules', [])):
                    with open(temp_rule_path, 'w') as temp_file:
                        json.dump([rule_dict], temp_file)
                    processor.add_rules_from_directory([], self._empty_rules_dirs)
                    self._eval_rule_test(rule_test, processor, idx)
                    processor._generic_rules.clear()
                    remove_file_if_exists(temp_rule_path)
        else:
            raise AutoRuleTesterException('No rules provided for processor of type {}'.format(processor.describe()))

    def _print_error_on_exception(self, error, rule_test, t_idx):
        self._print_filename(rule_test)
        print_fcolor(Fore.MAGENTA, f'RULE {t_idx}:')
        print_fcolor(Fore.RED, f'Exception: {error}')
        self._print_stack_trace(error)

    def _print_stack_trace(self, error):
        if self._enable_print_stack_trace:
            print('Stack Trace:')
            tb = traceback.format_tb(error.__traceback__)
            for line in tb:
                print(line)

    def _print_filename(self, rule_test):
        if not self._filename_printed:
            print_fcolor(Fore.LIGHTMAGENTA_EX, f'\nRULE FILE {rule_test["file"]}')
            self._filename_printed = True

    def _eval_rule_test(self, rule_test, processor, r_idx):
        self._filename_printed = False
        for t_idx, test in enumerate(rule_test['tests']):
            if test.get('target_rule_idx') is not None and test.get('target_rule_idx') != r_idx:
                continue

            processor.ps.setup_rules(['placeholder'])  # Setup arrays according to rule count, but here it's always one
            try:
                extra_output = processor.process(test['raw'])
            except BaseException as error:
                self._print_error_on_exception(error, rule_test, t_idx)
                self._success = False
                self._failed_rule_tests_cnt += 1
                return

            diff = self._get_diff_raw_test(test)
            print_diff = self._check_if_different(diff)

            errors = []
            warnings = []

            self._pd_extra.update_errors(processor, extra_output, errors, warnings)

            if print_diff or warnings or errors:
                self._print_filename(rule_test)
                print_fcolor(Fore.MAGENTA, f'RULE {t_idx}:')

            if print_diff:
                self._print_filename(rule_test)
                self._print_diff_test(diff)

            if print_diff or errors:
                self._success = False
                self._failed_rule_tests_cnt += 1
            else:
                self._successful_rule_tests_cnt += 1

            self._warning_cnt += len(warnings)

            self._print_errors_and_warnings(errors, warnings)

    @staticmethod
    def _print_errors_and_warnings(errors, warnings):
        for error in errors:
            print_fcolor(Fore.RED, error)

        for warning in warnings:
            print_fcolor(Fore.YELLOW, warning)

    @staticmethod
    def _check_if_different(diff):
        return any([item for item in diff if item.startswith(('+', '-', '?'))])

    @staticmethod
    def _check_which_rule_files_miss_tests(rules_pp):
        rules_with_tests = list()
        rules_without_tests = list()
        for rules in rules_pp.values():
            for rule in rules:
                if rule['tests']:
                    rules_with_tests.append(rule['file'])
                else:
                    rules_without_tests.append(rule['file'])

        print_fcolor(Fore.LIGHTGREEN_EX, '\nRULES WITH TESTS:')
        for rule in rules_with_tests:
            print_fcolor(Fore.LIGHTGREEN_EX, f'  {rule}')
        if not rules_with_tests:
            print_fcolor(Fore.LIGHTGREEN_EX, 'None')
        print_fcolor(Fore.LIGHTRED_EX, '\nRULES WITHOUT TESTS:')
        for rule in rules_without_tests:
            print_fcolor(Fore.LIGHTRED_EX, f'  {rule}')
        if not rules_without_tests:
            print_fcolor(Fore.LIGHTRED_EX, f'None')

    @staticmethod
    def _get_processor_instance_and_rule_type(name, processor_cfg, logger):
        cfg = {name: processor_cfg}
        processor = ProcessorFactory.create(cfg, logger)
        plugin_path = path.join(str(pathlib.Path(inspect.getfile(processor.__class__)).parent), 'rule.py')
        loaded_rule_classes_map = {inspect.getfile(rule_class): rule_class for rule_class in Rule.__subclasses__()}
        current_rule_class = loaded_rule_classes_map.get(plugin_path)
        if current_rule_class is None and isinstance(processor, RuleBasedProcessor):
            raise AutoRuleTesterException('Rule class missing for processor: {}'.format(processor.describe()))

        return processor, current_rule_class

    @staticmethod
    def _print_diff_test(diff):
        for item in diff:
            if item.startswith('- '):
                print_fcolor(Fore.RED, item)
            elif item.startswith('+ '):
                print_fcolor(Fore.GREEN, item)
            elif item.startswith('? '):
                print_fcolor(Fore.WHITE, item)
            else:
                print_fcolor(Fore.CYAN, item)

    def _get_diff_raw_test(self, test):
        self._gpl.replace_grok_keywords(test['processed'], test)

        raw = json.dumps(test['raw'], sort_keys=True, indent=4)
        processed = json.dumps(test['processed'], sort_keys=True, indent=4)

        diff = ndiff(raw.splitlines(), processed.splitlines())
        return list(diff)

    def _set_rules_dirs_to_empty(self):
        for processor in self._config_yml['pipeline']:
            processor_cfg = next(iter(processor.values()))

            if processor_cfg.get('rules'):
                processor_cfg['rules'] = self._empty_rules_dirs
            elif processor_cfg.get('generic_rules') and processor_cfg.get('specific_rules'):
                processor_cfg['generic_rules'] = self._empty_rules_dirs
                processor_cfg['specific_rules'] = self._empty_rules_dirs

    @staticmethod
    def _check_test_validity(errors, rule_tests, test_file):
        has_errors = False
        for rule_test in rule_tests:
            rule_keys = set(rule_test.keys())
            valid_keys = {'raw', 'processed', 'target_rule_idx'}
            required_keys = {'raw', 'processed'}
            invalid_keys = rule_keys.difference(valid_keys)
            has_error = False

            if invalid_keys.difference({'target_rule_idx'}):
                errors.append(
                    'Schema error in test "{}": "Remove keys: {}"'.format(test_file.name, invalid_keys))
                has_error = True

            available_required_keys = rule_keys.intersection(required_keys)
            if available_required_keys != required_keys:
                errors.append(
                    'Schema error in test "{}": "The following required keys are missing: {}"'.format(
                        test_file.name, required_keys.difference(available_required_keys)))
                has_error = True

            if not has_error:
                if not isinstance(rule_test.get('raw'), dict) or not isinstance(rule_test.get('processed'), dict):
                    errors.append(
                        'Schema error in test "{}": "Values of raw and processed must be dictionaries"'.format(
                            test_file.name))
                    has_error = True
                if {'target_rule_idx'}.intersection(rule_keys):
                    if not isinstance(rule_test.get('target_rule_idx'), int):
                        errors.append(
                            'Schema error in test "{}": "Value of target_rule_idx must be an integer"'.format(
                                test_file.name))
                        has_error = True
            has_errors = has_errors or has_error
        return has_errors

    def _get_rules_per_processor_type(self, rules_dirs):
        print_fcolor(Fore.YELLOW, '\nRULES DIRECTORIES:')
        rules = defaultdict(list)
        errors = []
        for processor_type, proc_rules_dirs in rules_dirs.items():
            print_fcolor(Fore.YELLOW, f'  {processor_type}:')
            for rule_dirs_type, rules_dirs_by_type in proc_rules_dirs.items():
                print_fcolor(Fore.YELLOW, f'    {rule_dirs_type}:')
                for rules_dir in rules_dirs_by_type:
                    print_fcolor(Fore.YELLOW, f'      {rules_dir}:')
                    for root, _, files in walk(rules_dir):
                        rule_files = [file for file in files if self._is_valid_rule_name(file)]
                        for file in rule_files:
                            with open(path.join(root, file), 'r') as rules_file:
                                try:
                                    multi_rule = list(safe_load_all(rules_file)) if file.endswith(
                                        '.yml') else json.load(rules_file)
                                except json.decoder.JSONDecodeError as error:
                                    raise AutoRuleTesterException(
                                        'JSON decoder error in rule "{}": "{}"'.format(rules_file.name, str(error)))
                                except YAMLError as error:
                                    raise AutoRuleTesterException(
                                        'YAML error in rule "{}": "{}"'.format(rules_file.name, str(error)))
                            test_path = path.join(root, ''.join([file.rsplit('.', maxsplit=1)[0], '_test.json']))
                            if path.isfile(test_path):
                                with open(test_path, 'r') as test_file:
                                    try:
                                        rule_tests = json.load(test_file)
                                    except json.decoder.JSONDecodeError as error:
                                        errors.append(
                                            'JSON decoder error in test "{}": "{}"'.format(test_file.name, str(error)))
                                        continue
                                    has_errors = self._check_test_validity(errors, rule_tests, test_file)
                                    if has_errors:
                                        continue
                            else:
                                rule_tests = list()
                            rules[processor_type].append(
                                {rule_dirs_type: multi_rule, 'tests': rule_tests, 'file': path.join(root, file)})
        if errors:
            for error in errors:
                print_fcolor(Fore.RED, error)
            exit(1)
        return rules

    @staticmethod
    def _is_valid_rule_name(file_name):
        return (file_name.endswith('.json') or file_name.endswith('.yml')) and not file_name.endswith('_test.json')

    def _get_rule_dirs_by_processor_type(self):
        rules_dirs = defaultdict(dict)
        for processor in self._config_yml['pipeline']:
            processor_cfg = next(iter(processor.values()))

            rules_to_add = list()
            print('\nProcessor Config:')
            pprint(processor_cfg)

            if processor_cfg.get('rules'):
                rules_to_add.append(('rules', processor_cfg['rules']))
            elif processor_cfg.get('generic_rules') and processor_cfg.get('specific_rules'):
                rules_to_add.append(('generic_rules', processor_cfg['generic_rules']))
                rules_to_add.append(('specific_rules', processor_cfg['specific_rules']))

            if not rules_dirs[processor_cfg['type']]:
                rules_dirs[processor_cfg['type']] = defaultdict(list)

            for rule_to_add in rules_to_add:
                rules_dirs[processor_cfg['type']][rule_to_add[0]] += rule_to_add[1]

        return rules_dirs
