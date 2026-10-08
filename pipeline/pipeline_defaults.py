"""Project scan defaults; installed LichtFeld UI preferences are unchanged."""
MAX_IMAGES = None
KEEP_PERCENT = 30.0
SAMPLE_FPS = 0.0
BLUR_RATIO = 0.90
MAX_GLARE = 0.20
FEATURES = 8192
MATCHES = 16384
BA_ITERATIONS = 100
RECONSTRUCTION_MODE = 'global'
CALIBRATE_INTRINSICS = True
EXPORT_FORMAT = 'sog'
DENSIFICATION = {'roma_setting': 'high', 'num_refs': 0.8, 'nns_per_ref': 3,
                'matches_per_ref': 10000, 'certainty_thresh': 0.20,
                'reproj_thresh': 0.8, 'sampson_thresh': 5.0,
                'min_parallax_deg': 0.5, 'min_track_length': 1,
                'chunked_batches': 4, 'seed': 0}
TRAINING = {'strategy': 'mrnf',
 'iterations': 30000,
 'means_lr': 2e-05,
 'means_lr_end': 2e-07,
 'shs_lr': 0.002,
 'opacity_lr': 0.012,
 'scaling_lr': 0.007,
 'scaling_lr_end': 0.005,
 'rotation_lr': 0.002,
 'lambda_dssim': 0.2,
 'min_opacity': 0.00392156862745098,
 'refine_every': 200,
 'start_refine': 0,
 'stop_refine': 33000,
 'grad_threshold': 0.003,
 'sh_degree': 3,
 'opacity_reg': 0.0,
 'scale_reg': 0.0,
 'revised_opacity': True,
 'use_error_map': True,
 'use_edge_map': True,
 'max_cap': 3000000,
 'growth_grad_threshold': 0.0015,
 'grow_fraction': 0.12,
 'grow_until_iter': 30000,
 'save_steps': [30000, 45000],
 'gut': True,
 'use_bilateral_grid': True,
 'mip_filter': True,
 'enable_sparsity': True,
 'sparsify_steps': 15000,
 'init_rho': 0.0005,
 'prune_ratio': 0.6,
 'undistort': True,
 'use_ppisp': True,
 'ppisp_use_controller': False,
 'ppisp_controller_activation_step': 35000,
 'ppisp_freeze_gaussians_on_distill': True}
