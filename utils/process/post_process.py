import rasterio.io
import os
from ..dataset import *
from ..agland_map import *
from tqdm import tqdm
from utils.io import *
from models import gbt
from utils.tools.census_core import load_census_table_pkl
from utils.tools.geo import crop_intermediate_state
from utils.tools.pycno_interp import pycno

BIAS_CORRECTION_ATTRIBUTES = ['BC_CROP', 'BC_PAST', 'BC_OTHE']


def make_nonagricultural_mask(water_body_mask_dir, gdd_filter_map_dir, shape):
    """
    Generate a non-agricultural boolean mask by merging water_body_mask and gdd_filter_map,
    both mask shall indicate 0 as non-agricultural regions and 1 otherwise

    Args:
        water_body_mask_dir (str): path directory to water body mask tif file
        gdd_filter_map_dir (str): path directory to gdd filter map tif file
        shape (tuple): (height, width) of the output mask shape

    Returns: (np.array) 2D boolean mask matrix
    """
    # Load maps
    water_body_mask_map = rasterio.open(water_body_mask_dir).read(1)
    gdd_filter_map = rasterio.open(gdd_filter_map_dir).read(1)

    # Resize two maps to match the input shape
    # Use nearest neighbors as interpolation method
    water_body_mask_map_scaled = cv2.resize(water_body_mask_map,
                                            dsize=(shape[1], shape[0]),
                                            interpolation=cv2.INTER_NEAREST)
    gdd_filter_map_scaled = cv2.resize(gdd_filter_map,
                                       dsize=(shape[1], shape[0]),
                                       interpolation=cv2.INTER_NEAREST)

    return np.multiply(water_body_mask_map_scaled, gdd_filter_map_scaled)


def check_weights_exists(deploy_setting_cfg, iter):
    """
    Check if bias correction weights arrays for iter exist in the path defined in 
    deploy_setting_cfg['path_dir']['base']

    Args:
        deploy_setting_cfg (dict): deploy settings from yaml
        iter (int): iter index
    
    Returns: (boolean)
    """
    base_path = deploy_setting_cfg['path_dir']['base']
    for attribute in BIAS_CORRECTION_ATTRIBUTES:
        if not os.path.exists(os.path.join(base_path, attribute + '_' + str(int(iter)) + '.npy')):
            return False
    
    return True


def load_weights_array(deploy_setting_cfg, iter):
    """
    Load bias correction weights arrays for iter specified in the path defined in 
    deploy_setting_cfg['path_dir']['base']

    Args:
        deploy_setting_cfg (dict): deploy settings from yaml
        iter (int): iter index

    Returns: (tuple) 2D weights arrays tuple, (crop, past, other)
    """
    base_path = deploy_setting_cfg['path_dir']['base']
    weight_array_list = []
    for attribute in BIAS_CORRECTION_ATTRIBUTES:
        weight_array_list.append(np.load(os.path.join(base_path, attribute + '_' + str(int(iter)) + '.npy')))

    return (*weight_array_list, )


def generate_weights_array(deploy_setting_cfg, input_dataset, agland_map, iter=0):
    """
    Generate bias correction weights arrays by back correcting input agland_map to 
    match the input_dataset, followed by a probability distribution fix (scale), and 
    a pycno interpolation based on settings in deploy_setting_cfg['post_process']['interpolation'] 
    for smoothing boundary effects

    Args:
        deploy_setting_cfg (dict): deploy settings from yaml
        input_dataset (Dataset): input dataset for training
        agland_map (AglandMap): input agland_map to be corrected
        iter (int): iter index. Default: 0

    Returns: (tuple) 2D weights arrays tuple, (crop, past, other)
    """
    base_path = deploy_setting_cfg['path_dir']['base']
    grid_size = agland_map.affine[0]
    x_min = agland_map.affine[2]
    y_max = agland_map.affine[5]

    cropland_map = agland_map.get_cropland().copy()
    pasture_map = agland_map.get_pasture().copy()
    other_map = agland_map.get_other().copy()

    bc_factor_cropland = np.zeros((len(input_dataset.census_table)))
    bc_factor_pasture = np.zeros((len(input_dataset.census_table)))
    bc_factor_other = np.zeros((len(input_dataset.census_table)))

    for i in tqdm(range(len(input_dataset.census_table))):
        # Crop intermediate samples with nodata to be -1
        out_cropland = crop_intermediate_state(cropland_map,
                                               agland_map.affine,
                                               input_dataset, i)
        out_pasture = crop_intermediate_state(pasture_map,
                                              agland_map.affine,
                                              input_dataset, i)
        out_other = crop_intermediate_state(other_map,
                                            agland_map.affine,
                                            input_dataset, i)

        ground_truth_cropland = input_dataset.census_table.iloc[i][
            'CROPLAND_PER']
        ground_truth_pasture = input_dataset.census_table.iloc[i][
            'PASTURE_PER']
        ground_truth_other = input_dataset.census_table.iloc[i]['OTHER_PER']

        mask_index_cropland = np.where(out_cropland != -1)
        mask_index_pasture = np.where(out_pasture != -1)
        mask_index_other = np.where(out_other != -1)

        mean_pred_cropland = np.mean(out_cropland[mask_index_cropland])
        mean_pred_pasture = np.mean(out_pasture[mask_index_pasture])
        mean_pred_other = np.mean(out_other[mask_index_other])

        # If average values is found to be 0 that means the state level is not
        # presented in agland map. This is due to the change in resolution from census_table
        # to agland map (high res -> low res). For these cases, factor is set to
        # be 1
        if mean_pred_cropland != 0:
            bias_correction_factor_cropland = ground_truth_cropland / mean_pred_cropland
        else:
            bias_correction_factor_cropland = 1

        if mean_pred_pasture != 0:
            bias_correction_factor_pasture = ground_truth_pasture / mean_pred_pasture
        else:
            bias_correction_factor_pasture = 1

        if mean_pred_other != 0:
            bias_correction_factor_other = ground_truth_other / mean_pred_other
        else:
            bias_correction_factor_other = 1

        bc_factor_cropland[i] = bias_correction_factor_cropland
        bc_factor_pasture[i] = bias_correction_factor_pasture
        bc_factor_other[i] = bias_correction_factor_other

    # Add bc_factors to census table as weights table
    census_table = GeoDataFrame(input_dataset.census_table, crs=4326)
    weights_table = census_table[[
        'STATE', 'GID_0', 'REGIONS', 'geometry', 'AREA'
    ]].copy()

    weights_table[BIAS_CORRECTION_ATTRIBUTES[0]] = list(bc_factor_cropland)
    weights_table[BIAS_CORRECTION_ATTRIBUTES[1]] = list(bc_factor_pasture)
    weights_table[BIAS_CORRECTION_ATTRIBUTES[2]] = list(bc_factor_other)
    weights_table = weights_table.fillna(1)  # replace any nan in weights table by 1

    # Apply pycno interpolation over weights arrays
    weight_array_list = []
    for attribute in BIAS_CORRECTION_ATTRIBUTES:
        weights_array = pycno(gdf=weights_table, 
                            value_field=attribute, 
                            x_min=x_min, 
                            y_max=y_max,
                            pixel_size=grid_size,
                            converge=deploy_setting_cfg['post_process']['interpolation']['converge'],
                            r=deploy_setting_cfg['post_process']['interpolation']['r'], 
                            seperable_filter=deploy_setting_cfg['post_process']['interpolation']['seperable_filter'], 
                            verbose=True)
                            
        weights_file_dir = os.path.join(base_path, attribute + '_' + str(int(iter)) + '.npy')
        weight_array_list.append(weights_array[0])
        np.save(weights_file_dir, weights_array[0])
        print('{} saved'.format(weights_file_dir))

    return (*weight_array_list, )


def apply_bias_correction_to_agland_map(agland_map,
                                        bc_crop, bc_past, bc_other,
                                        correction_method='scale', iter=0):
    """
    Bias correct the input AglandMap obj to match the state-level samples in input_dataset.
    This process does not guarantee a perfect match, as the outputs will break the probability
    distribution after each iteration of correction. Then correction_method is called to
    force each modified values in the 3 agland map to probability distribution

    Args:
        agland_map (AglandMap): input agland_map to be corrected
        bc_crop (np.array): weights for cropland
        bc_past (np.array): weights for pasture
        bc_other (np.array): weights for other
        correction_method (str): 'scale' ('softmax' does not provide good results)
        iter (int): iter index

    Returns: (AglandMap)
    """
    return AglandMap(agland_map.get_cropland()*bc_crop, 
                     agland_map.get_pasture()*bc_past, 
                     agland_map.get_other()*bc_other, force_load=True)


def pipeline(deploy_setting_cfg, land_cover_cfg, training_cfg):
    """
    Deploy pipeline:
    1. Load pre-trained model, land cover inputs, inputs census data
    2. Run prediction on pre-trained model weights on inputs to get an initial agland map
    3. Apply bias correction and pycno interpolation on weights iteratively

    Args:
        deploy_setting_cfg (dict): deploy settings from yaml
        land_cover_cfg (dict): land cover settings from yaml
        training_cfg (dict): training settings from yaml
    """
    # Load land cover counts histogram map
    land_cover_counts = load_pkl(
        land_cover_cfg['path_dir']['pred_input_map'][:-len('.pkl')])
    output_height, output_width = int(max(land_cover_counts.census_table['ROW_IDX']) + 1), \
                                  int(max(land_cover_counts.census_table['COL_IDX']) + 1),

    # Load model
    prob_est = gbt.GradientBoostingTree(
        ntrees=training_cfg['model']['gradient_boosting_tree']['ntrees'],
        max_depth=training_cfg['model']['gradient_boosting_tree']['max_depth'],
        nfolds=training_cfg['model']['gradient_boosting_tree']['nfolds'],
        distribution=training_cfg['model']['gradient_boosting_tree']
        ['distribution'])
    try:
        prob_est.load(deploy_setting_cfg['path_dir']['model'])
        print('Model loaded from {}'.format(
            deploy_setting_cfg['path_dir']['model']))
    except h2o.exceptions.H2OResponseError:
        raise h2o.exceptions.H2OResponseError(
            'File {} is not valid model path.'.format(
                deploy_setting_cfg['path_dir']['model']))

    # Initial deployment
    output_prob = prob_est.predict(land_cover_counts).to_numpy()
    initial_agland_map = AglandMap(
        output_prob[:, 0].reshape(output_height, output_width),
        output_prob[:, 1].reshape(output_height, output_width),
        output_prob[:, 2].reshape(output_height, output_width),
        force_load=True)

    # Save initial results
    initial_agland_map.save_as_tif(
        deploy_setting_cfg['path_dir']['agland_map_output'][:-len('.tif')] +
        '_0' + '.tif')

    # Load input dataset for bias correction step
    input_dataset = Dataset(
        census_table=load_census_table_pkl(
            deploy_setting_cfg['path_dir']['census_table_input']),
        land_cover_code=land_cover_cfg['code']['MCD12Q1'],
        remove_land_cover_feature_index=deploy_setting_cfg['feature_remove'])

    # Bias correction iterator
    for i in range(deploy_setting_cfg['post_process']['correction']['itr']):
        # Load previous agland map
        print('Bias Correction itr: {}/{}'.format(
            i, deploy_setting_cfg['post_process']['correction']['itr']))
        intermediate_agland_map = load_tif_as_AglandMap(
            (deploy_setting_cfg['path_dir']['agland_map_output'][:-len('.tif')]
             + '_{}' + '.tif').format(str(i)),
            force_load=True)

        # Do bias correction
        if check_weights_exists(deploy_setting_cfg, i):
            print('Bias correction weights loaded')
            bc_crop, bc_past, bc_other = load_weights_array(
                deploy_setting_cfg, i)
        else:
            print('Generate new bias correction weights')
            bc_crop, bc_past, bc_other = generate_weights_array(
                deploy_setting_cfg, input_dataset, intermediate_agland_map, i)

        intermediate_agland_map = apply_bias_correction_to_agland_map(
            intermediate_agland_map, bc_crop, bc_past, bc_other,
            deploy_setting_cfg['post_process']['correction']['method'], i)

        # Save current intermediate results
        intermediate_agland_map.save_as_tif(
            (deploy_setting_cfg['path_dir']['agland_map_output'][:-len('.tif')]
             + '_{}' + '.tif').format(i + 1))

