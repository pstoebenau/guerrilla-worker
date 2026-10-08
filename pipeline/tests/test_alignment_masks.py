from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing
import cv2
import numpy as np
from alignment_masks import audit_features, mask_files, unmasked_training


class AlignmentMasksTest(unittest.TestCase):
    def test_feature_exclusion_and_training_separation(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            image = root/'frame.png'
            cv2.imwrite(str(image), np.zeros((10, 10, 3), np.uint8))
            masks = root/'alignment'; masks.mkdir()
            mask = np.full((10, 10), 255, np.uint8); mask[:5, :5] = 0
            cv2.imwrite(str(masks/'frame.png.png'), mask)
            self.assertEqual(mask_files([image], masks), [masks/'frame.png.png'])
            db = root/'features.db'
            with closing(sqlite3.connect(db)) as connection, connection:
                connection.executescript('CREATE TABLE images(image_id INTEGER, name TEXT);'
                                         'CREATE TABLE keypoints(image_id INTEGER, rows INTEGER, cols INTEGER, data BLOB);')
                connection.execute('INSERT INTO images VALUES(1, ?)', (image.name,))
                connection.execute('INSERT INTO keypoints VALUES(1, 1, 2, ?)',
                                   (np.array([[7.5, 7.5]], np.float32).tobytes(),))
            self.assertEqual(audit_features(db, masks, [image])['masked_features'], 0)
            with closing(sqlite3.connect(db)) as connection, connection:
                connection.execute('UPDATE keypoints SET data=?',
                                   (np.array([[2.5, 2.5]], np.float32).tobytes(),))
            with self.assertRaisesRegex(ValueError, 'inside exclusion masks'):
                audit_features(db, masks, [image])
            config, args = unmasked_training(root, {'max_cap': 5000000})
            self.assertEqual(config['mask_mode'], 'none')
            self.assertEqual(args, ['--mask-mode', 'none'])
            (root/'masks').mkdir()
            with self.assertRaisesRegex(ValueError, 'training dataset'):
                unmasked_training(root, config)


if __name__ == '__main__':
    unittest.main()
