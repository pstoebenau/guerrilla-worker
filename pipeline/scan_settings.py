"""Per-scan options shared with the web form through JSON Schema."""
from __future__ import annotations

import json
from pathlib import Path

from pipeline_defaults import TRAINING, DENSIFICATION

SCHEMA_PATH = Path(__file__).with_name('scan-settings.schema.json')


def defaults():
    schema = json.loads(SCHEMA_PATH.read_text(encoding='utf-8'))
    return {group: {key: field['default'] for key, field in section['properties'].items()}
            for group, section in schema['properties'].items()}


def load(path=None, *, validate=True):
    result = defaults()
    def invalid_constant(value):
        raise ValueError(f'Invalid JSON number: {value}')
    supplied = json.loads(Path(path).read_text(encoding='utf-8'), parse_constant=invalid_constant) if path else {}
    if validate:
        from jsonschema import Draft7Validator
        schema = json.loads(SCHEMA_PATH.read_text(encoding='utf-8'))
        Draft7Validator(schema).validate(supplied)
    for group, values in supplied.items():
        result[group].update(values)
    schema = json.loads(SCHEMA_PATH.read_text(encoding='utf-8'))
    for group, values in result.items():
        for key, value in values.items():
            kind = schema['properties'][group]['properties'][key]['type']
            if value is not None and (kind == 'integer' or isinstance(kind, list) and 'integer' in kind):
                values[key] = int(value)
    selection = result['selection']
    if selection['end_seconds'] is not None and selection['end_seconds'] <= selection['start_seconds']:
        raise ValueError('End time must be greater than start time')
    if selection['preserve_coverage'] and selection['max_images'] is not None:
        raise ValueError('Preserve coverage cannot be combined with a maximum frame count')
    return result


def training(options, max_cap):
    config = {**TRAINING, **options['training'], 'max_cap': max_cap}
    iterations = config['iterations']
    # Preserve the established schedule proportions when changing duration.
    config['grow_until_iter'] = iterations
    config['stop_refine'] = round(iterations * TRAINING['stop_refine'] / TRAINING['iterations'])
    end = iterations + (config['sparsify_steps'] if config['enable_sparsity'] else 0)
    config['save_steps'] = sorted({iterations, end})
    return config


def densification(options):
    return {**DENSIFICATION, **options['densification']}


def selection_arguments(options):
    arguments = []
    for key, value in options['selection'].items():
        flag = '--' + key.replace('_', '-')
        if value is True:
            arguments.append(flag)
        elif value is not None and value is not False:
            arguments.extend([flag, str(value)])
    return arguments
