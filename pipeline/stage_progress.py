"""Translate native engine diagnostics into bounded, user-facing progress fields."""
import math
import re


DESCRIPTIONS = {
    'selection': 'Selecting sharp, well-spaced frames',
    'reconstruction': 'Reconstructing cameras and scene geometry',
    'densification': 'Building the initial dense point cloud',
    'training': 'Loading the dataset and preparing training',
    'export': 'Creating and verifying scene exports',
}
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
NUMBER = r'([\d,]+(?:\.\d+)?(?:[eE][+-]?\d+)?)'


def seconds(value):
    parts = re.findall(r'\d+(?:\.\d+)?', value)
    return sum(float(part) * 60 ** index for index, part in enumerate(reversed(parts)))


def parse_progress(stage, text):
    """Unknown lines never become UI text; counters describe the named substep."""
    result = {}
    for line in re.split(r'[\r\n]+', ANSI.sub('', text)):
        update = {}
        if stage == 'training':
            # LichtFeld inserts a Unicode bar, percentage and timer before N/M.
            match = re.search(r'\b(?:Training|step|iter(?:ation)?)\b.*?(\d[\d,]*)\s*/\s*(\d[\d,]*)', line, re.I)
            if match:
                update = dict(substep='Optimizing Gaussians', unit='iterations',
                              current=int(match[1].replace(',', '')), total=int(match[2].replace(',', '')))
                eta = re.search(r'ETA\s+([\d:.]+)|<([\d:hm.s]+)\]', line, re.I)
                if eta:
                    update['remainingSeconds'] = seconds(eta[1] or eta[2])
                for key, pattern in [('gaussians', r'\b(?:splats|gaussians)\s*[:=]?\s*'),
                                     ('loss', r'\b(?:rgb_loss|loss)\s*[:=]\s*'),
                                     ('psnr', r'\bpsnr\s*[:=]\s*')]:
                    metric = re.search(pattern + NUMBER, line, re.I)
                    if metric:
                        value = float(metric[1].replace(',', ''))
                        if math.isfinite(value):
                            update[key] = int(value) if key == 'gaussians' else value
            if re.search(r'Training completed|Training complete\.', line, re.I):
                update = dict(substep='Saving trained scene')
            final = re.search(r'Final splats:\s*([\d,]+)', line, re.I)
            if final:
                update.update(substep='Saving trained scene', gaussians=int(final[1].replace(',', '')))
        elif stage == 'selection':
            for pattern, label, unit in [
                (r'scanned (\d+)/(\d+) frames', 'Scoring video frames', 'frames'),
                (r'Exported (\d+)/(\d+) (?:PNGs|JPEGs)', 'Exporting selected frames', 'images'),
            ]:
                match = re.search(pattern, line, re.I)
                if match:
                    update = dict(substep=label, current=int(match[1]), total=int(match[2]), unit=unit)
            if 'Selecting from' in line:
                update = dict(substep='Choosing distinct, sharp views')
        elif stage == 'densification':
            match = re.search(r'\[\s*(\d+(?:\.\d+)?)%\]\s*(.*)', line)
            if match:
                message = match[2].lower()
                label = ('Merging dense point chunks' if 'merg' in message else
                         'Writing dense point cloud' if 'writing' in message or 'done!' in message else
                         'Matching image pairs' if 'matching' in message else 'Preparing dense reconstruction')
                update = dict(substep=label, current=float(match[1]), total=100, unit='percent')
                chunk = re.search(r'chunk (\d+)/(\d+)', message)
                if chunk:
                    update['substep'] = f'Matching image pairs · chunk {chunk[1]} of {chunk[2]}'
        elif stage == 'reconstruction':
            for pattern, label in [
                (r'extracting features|feature extraction|\[extract\]', 'Extracting image features'),
                (r'matching|\[match\]', 'Matching image features'),
                (r'triangulat|registering|\[map\]', 'Reconstructing camera poses'),
                (r'bundle adjustment', 'Refining cameras and scene geometry'),
                (r'\[orient\]', 'Orienting the scene'),
            ]:
                if re.search(pattern, line, re.I):
                    update = dict(substep=label)
            match = re.search(r'(?:Processed file|Extracted features for image)\s*\[?(\d+)\s*/\s*(\d+)', line, re.I)
            if match:
                update = dict(substep='Extracting image features', current=int(match[1]), total=int(match[2]), unit='images')
        if update:
            if 'total' in update and not 0 <= update['current'] <= update['total']:
                continue
            if update.get('substep') != result.get('substep'):
                result = update
            else:
                result.update(update)
    return result
