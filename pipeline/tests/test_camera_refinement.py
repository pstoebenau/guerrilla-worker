import tempfile
from pathlib import Path
import unittest
import numpy as np
import camera_refinement as cr
try:
    import pycolmap as pc
except ImportError:
    pc = None

@unittest.skipIf(pc is None, 'Requires the installed pycolmap runtime')
class CameraRefinementTest(unittest.TestCase):
    def test_real_model_preserves_observations_and_validates_before_publish(self):
        cr.pc = pc
        model = pc.Reconstruction()
        model.add_camera_with_trivial_rig(pc.Camera(camera_id=1, model='PINHOLE', width=800, height=600, params=[700,700,400,300]))
        rng = np.random.default_rng(42)
        points = rng.uniform([-1,-.7,3],[1,.7,5],(120,3))
        for i in range(8):
            pose=pc.Rigid3d(pc.Rotation3d(),np.array([i*.1-.35,0.,0.]))
            xy=model.cameras[1].img_from_cam(points+pose.translation)
            model.add_image_with_trivial_frame(pc.Image(name=f'{i}.png',keypoints=xy,camera_id=1,image_id=i+1),pose)
        for j,xyz in enumerate(points):
            track=pc.Track()
            for i in range(8):track.add_element(i+1,j)
            model.add_point3D(xyz,track,np.array([100,100,100],dtype=np.uint8))
        clone=cr.per_frame(model)
        self.assertTrue(clone.is_valid())
        self.assertEqual(clone.num_cameras(),8)
        self.assertEqual(clone.compute_num_observations(),960)
        for i in model.reg_image_ids():
            np.testing.assert_allclose(clone.images[i].cam_from_world().matrix(),model.images[i].cam_from_world().matrix())
        with tempfile.TemporaryDirectory() as temp:
            sparse=Path(temp)/'sparse';sparse.mkdir();model.write(str(sparse))
            report=cr.refine_dataset(sparse,Path(temp)/'report.json')
            self.assertEqual(report['validation_observations'],120)
            result=pc.Reconstruction(str(sparse))
            self.assertTrue(result.is_valid())
            self.assertEqual(result.compute_num_observations(),960)

if __name__ == '__main__': unittest.main()
