from census_processor.country import *


def subnational_processor_latvia(subnational_dir):
    """
    Customized subnational_stats data processor for Latvia files
    Note: Output pd.Dataframe must only contain STATE | CROPLAND | PASTURE

    Args:
        subnational_dir (str): path dir for EU country collection .xlsx file

    Returns: (pd) dataframe that contains only STATE | CROPLAND | PASTURE
    """
    # cropland - Arable land + Permanent crops
    # pasture - Permanent grassland - outdoor
    raw_subnation_data = pd.read_excel(subnational_dir, header=11, sheet_name='Sheet 1', skipfooter=6)
    raw_subnation_data = raw_subnation_data[
        ['CROPS (Labels)', 'Arable land', 'Permanent crops', 'Permanent grassland - outdoor']]

    # Modify here for EU Countries
    num_states = 1
    start_index = raw_subnation_data.index[raw_subnation_data['CROPS (Labels)'] == 'Latvia'][0] + 1
    end_index = start_index + num_states

    raw_subnation_data = raw_subnation_data.iloc[start_index:end_index, :]
    subnation_data = pd.DataFrame(columns=['STATE', 'CROPLAND', 'PASTURE'])
    subnation_data['STATE'] = raw_subnation_data['CROPS (Labels)']
    subnation_data['CROPLAND'] = raw_subnation_data['Arable land'] + raw_subnation_data['Permanent crops']
    subnation_data['PASTURE'] = raw_subnation_data['Permanent grassland - outdoor']

    # Rename states in census to match GADM
    subnation_data = subnation_data.replace({'Latvija': 'LATVIA'}, inplace=False)

    return subnation_data


class Latvia(Country):

    def __init__(self, shapefile_dir, subnational_dir, FAOSTAT_dir, units='Ha'):
        """
        Constructor that takes directory of the countries' shapefile,
        subnational data and FAOSTAT

        Args:
            shapefile_dir (str): shapefile dir for this country
            subnational_dir (list of str): subnational_stats dir for EU country
            FAOSTAT_dir (str): global FAOSTAT dir
            units (str): units used in the subnational file (Default: Ha)
        """
        assert units in Country.UNIT_LOOKUP, \
            "Unit: [" + units + "] is not found in the dict. Consider adding it to the class or check your input"

        self.FAOSTAT = self.get_FAOSTAT(FAOSTAT_dir)
        self.spatial_map = self.get_spatial_map(shapefile_dir)
        self.subnational_data = self.get_subnational_data(subnational_processor_latvia, *subnational_dir)
        self.units = units

    def get_spatial_map(self, shapefile_dir):
        # Cyprus data only contains country level info
        spatial_map = Country.switch_case(gpd.read_file(shapefile_dir), 'upper')
        spatial_map = spatial_map.rename(columns={'COUNTRY': 'STATE'}, inplace=False)

        return spatial_map
