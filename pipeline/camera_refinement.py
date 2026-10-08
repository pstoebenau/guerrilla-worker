"""Validate per-frame intrinsics before applying them to a fresh undistorted dataset."""
from pathlib import Path
import json
import numpy as np

def observations(model):
    result = {}
    for iid in model.reg_image_ids():
        im = model.images[iid]
        idx = np.array(im.get_observation_point2D_idxs(), dtype=int)
        result[iid] = (idx, np.array([im.points2D[int(j)].point3D_id for j in idx], dtype=np.int64),
                       np.array([im.points2D[int(j)].xy for j in idx]))
    return result

def errors(model, obs):
    result = {}
    registered = set(model.reg_image_ids())
    for iid, (idx, ids, xy) in obs.items():
        if iid not in registered: continue
        if not len(ids): continue
        keep = np.array([model.exists_point3D(int(pid)) for pid in ids])
        ids2 = ids[keep]
        if not len(ids2): continue
        im = model.images[iid]
        xyz = np.array([model.points3D[int(pid)].xyz for pid in ids2])
        cam = im.cam_from_world()
        transformed = xyz @ cam.rotation.matrix().T + cam.translation
        uv = model.cameras[im.camera_id].img_from_cam(transformed)
        err = np.linalg.norm(uv-xy[keep], axis=1)
        err[(transformed[:,2] <= 0) | ~np.isfinite(err)] = 10000
        result[iid] = (idx[keep], ids2, err)
    return result

def stats(e):
    a = np.concatenate([v[2] for v in e.values()])
    return dict(observations=len(a), mean=float(a.mean()), median=float(np.median(a)),
                p90=float(np.percentile(a,90)), p95=float(np.percentile(a,95)),
                under_1px=float(np.mean(a<1)), under_2px=float(np.mean(a<2)))

def per_frame(model):
    result=pc.Reconstruction()
    for iid in model.reg_image_ids():
        im=model.images[iid];oldcam=model.cameras[im.camera_id]
        cam=pc.Camera(camera_id=iid,model='PINHOLE',width=oldcam.width,height=oldcam.height,params=oldcam.params)
        result.add_camera_with_trivial_rig(cam)
        image=pc.Image(name=im.name,keypoints=np.array([p.xy for p in im.points2D]),camera_id=iid,image_id=iid)
        result.add_image_with_trivial_frame(image,im.cam_from_world())
    for pid,p in model.points3D.items():
        result.add_point3D_with_id(pid,pc.Point3D(xyz=p.xyz,color=p.color,track=p.track,error=p.error))
    assert result.is_valid()
    return result

def refine(model,loss,vary_focal=False,vary_center=False):
    opts=pc.BundleAdjustmentOptions();opts.refine_focal_length=vary_focal
    opts.refine_principal_point=vary_center;opts.refine_extra_params=False
    opts.ceres.loss_function_type=getattr(pc.LossFunctionType,loss)
    opts.ceres.loss_function_scale=1.;opts.ceres.use_gpu=True
    opts.ceres.solver_options.num_threads=8
    opts.ceres.solver_options.max_num_iterations=100 if vary_center else 150
    opts.ceres.solver_options.function_tolerance=1e-8
    pc.bundle_adjustment(model,opts)

def heldout(source):
    rows={i:([],[],[]) for i in source.reg_image_ids()}
    rng=np.random.default_rng(20260929)
    for pid in sorted(source.points3D):
        track=source.points3D[pid].track.elements
        if len(track)<6: continue
        el=track[int(rng.integers(len(track)))]; row=rows[el.image_id]
        row[0].append(el.point2D_idx);row[1].append(pid)
        row[2].append(source.images[el.image_id].points2D[el.point2D_idx].xy.tolist())
    return {i:tuple(np.array(v,dtype=int if k<2 else float) for k,v in enumerate(row)) for i,row in rows.items() if row[0]}

def remove_holdout(model,hold):
    for iid,(indices,_,_) in hold.items():
        for idx in indices: model.delete_observation(iid,int(idx))

def refine_dataset(sparse, report_path):
    global pc
    import pycolmap as pc
    sparse=Path(sparse)
    source=pc.Reconstruction(str(sparse))
    hold=heldout(source)
    report={'validation_observations':sum(len(r[0]) for r in hold.values()),'accepted':False}
    if report['validation_observations'] < 100:
        report['reason']='Insufficient long-track observations for validation; shared calibration retained.'
    else:
        baseline=pc.Reconstruction(str(sparse));remove_holdout(baseline,hold)
        refine(baseline,'TRIVIAL',False,False)
        candidate=per_frame(source);remove_holdout(candidate,hold)
        refine(candidate,'TRIVIAL',True,True)
        before=stats(errors(baseline,hold));after=stats(errors(candidate,hold))
        camera=next(iter(source.cameras.values()));initial=camera.params
        def plausible(model):
            values=np.array([c.params for c in model.cameras.values()])
            return bool(np.all(np.abs(values[:,:2]/initial[:2]-1)<.05) and
                        np.all(np.abs(values[:,2:]-initial[2:])<np.array([camera.width,camera.height])*.05))
        accepted=(after['observations']==before['observations'] and after['median']<before['median']*.98
                  and after['mean']<=before['mean']*1.01 and after['p90']<=before['p90']*1.01 and plausible(candidate))
        report.update(baseline=before,candidate=after)
        if accepted:
            final=per_frame(source);refine(final,'TRIVIAL',True,True)
            if plausible(final) and final.is_valid():
                final.update_point_3d_errors();final.write(str(sparse))
                report.update(accepted=True,final=stats(errors(final,observations(final))))
            else: report['reason']='Full-data fit exceeded intrinsic bounds; shared calibration retained.'
        else: report['reason']='Per-frame calibration did not meet validation and intrinsic bounds.'
    Path(report_path).write_text(json.dumps(report,indent=2),encoding='utf-8')
    print('Per-frame camera refinement:',report,flush=True)
    return report
