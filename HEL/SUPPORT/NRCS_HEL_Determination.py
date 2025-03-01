from getpass import getuser
from math import pi
from os import path
from sys import exit
from time import ctime

from arcpy import CheckExtension, CheckOutExtension, CreateScratchName, Describe, env, Exists, GetInstallInfo, \
    GetParameter, GetParameterAsText, ListFields, Reclassify_3d, SetProgressorLabel
from arcpy.analysis import Clip, Intersect, Statistics
from arcpy.conversion import FeatureToRaster, RasterToPolygon
from arcpy.da import SearchCursor, UpdateCursor
from arcpy.management import AddField, CalculateField, CopyFeatures, CreateFileGDB, Delete, DeleteField, \
    Dissolve, JoinField, MultipartToSinglepart, PivotTable
from arcpy.mp import ArcGISProject, LayerFile
from arcpy.sa import ATan, Con, Cos, Divide, Fill, FlowDirection, FlowLength, FocalStatistics, IsNull, NbrRectangle, \
    Power, SetNull, Slope, Sin, TabulateArea, Times

from extract_DEM_by_CLU import extractDEM
from hel_utils import addLyrxByConnectionProperties, AddMsgAndPrint, deleteScratchLayers, errorMsg, removeMapLayers


class NoProcesingExit(Exception):
    pass


def logBasicSettings(textFilePath, helLayer, inputDEM, zUnits, use_runoff_ls):
    with open(textFilePath, 'a+') as f:
        f.write('\n######################################################################\n')
        f.write('Executing Tool: HEL Determination\n')
        f.write(f"User Name: {getuser()}\n")
        f.write(f"Date Executed: {ctime()}\n")
        f.write('User Parameters:\n')
        f.write(f"\tSelected HEL Layer: {helLayer}\n")
        f.write(f"\tInput DEM: {inputDEM}\n")
        f.write(f"\tDEM Elevation Units: {zUnits}\n")
        f.write(f"\tUse REQ Equation: {use_runoff_ls}\n")


### Initial Tool Validation ###
try:
    aprx = ArcGISProject('CURRENT')
    map = aprx.listMaps('HEL Determination')[0]
except:
    AddMsgAndPrint('This tool must be run from an ArcGIS Pro project that was developed from the template distributed with this toolbox. Exiting!', 2)
    exit()

if CheckExtension('Spatial') == 'Available':
    CheckOutExtension('Spatial')
else:
    AddMsgAndPrint('Spatial Analyst Extension not enabled. Please enable Spatial Analyst from Project, Licensing, Configure licensing options. Exiting...', 2)
    exit()


### Input Parameters ###
cluLayer = GetParameter(0)
helLayer = GetParameter(1)
inputDEM = GetParameter(2)
zUnits = GetParameterAsText(3)
use_runoff_ls = GetParameter(4)


### Set Variables for Soil Data Fields and Validate ###
for field in ListFields(helLayer):
    # Required Fields:
    if field.name.lower() == 'muhelcl':
        hel_field = field.name
    if field.name.lower() == 'musym':
        musym_field = field.name
    if field.name.lower() == 'k':
        k_field = field.name
    if field.name.lower() == 't':
        t_field = field.name
    if field.name.lower() == 'r':
        r_field = field.name
    # Optional Fields:
    muname_field = field.name if field.name.lower() == 'muname' else None
    muwat_field = field.name if field.name.lower() == 'muwathel' else None
    muwnd_field = field.name if field.name.lower() == 'muwndhel' else None

if not hel_field or not musym_field or not k_field or not t_field or not r_field:
    AddMsgAndPrint('\Missing one or more required fields in input soil layer. Exiting...', 2)
    exit()

### Set Local Variables and Paths ###
base_dir = path.abspath(path.dirname(__file__)) #\SUPPORT
scratch_gdb = path.join(base_dir, 'scratch.gdb')
support_gdb = path.join(base_dir, 'SUPPORT.gdb')
lu_table = path.join(support_gdb, 'lut_census_fips')
scratchLayers = list()

### Set LayerFiles Based on Pro Version ###
pro_version = GetInstallInfo()['Version']
layer_files_dir = path.join(base_dir, 'layer_files')
if pro_version[0] == '3':
    field_determination_lyrx = LayerFile(path.join(layer_files_dir, 'Field_Determination.lyrx')).listLayers()[0]
    final_hel_summary_lyrx = LayerFile(path.join(layer_files_dir, 'Final_HEL_Summary.lyrx')).listLayers()[0]
    initial_hel_summary_lyrx = LayerFile(path.join(layer_files_dir, 'Initial_HEL_Summary.lyrx')).listLayers()[0]
    lidar_hel_summary_lyrx = LayerFile(path.join(layer_files_dir, 'LiDAR_HEL_Summary.lyrx')).listLayers()[0]
if pro_version[0] == '2':
    field_determination_lyrx = LayerFile(path.join(layer_files_dir, 'Field_Determination_2.lyrx')).listLayers()[0]
    final_hel_summary_lyrx = LayerFile(path.join(layer_files_dir, 'Final_HEL_Summary_2.lyrx')).listLayers()[0]
    initial_hel_summary_lyrx = LayerFile(path.join(layer_files_dir, 'Initial_HEL_Summary_2.lyrx')).listLayers()[0]
    lidar_hel_summary_lyrx = LayerFile(path.join(layer_files_dir, 'LiDAR_HEL_Summary_2.lyrx')).listLayers()[0]

helc_fd = path.dirname(Describe(cluLayer).catalogPath)
helc_gdb = path.dirname(helc_fd)
fieldDetermination = path.join(helc_gdb, helc_fd, 'Field_Determination')
helSummary = path.join(helc_gdb, helc_fd, 'Initial_HEL_Summary')
finalHELSummary = path.join(helc_gdb, helc_fd, 'Final_HEL_Summary')
lidarHEL = path.join(helc_gdb, 'LiDAR_HEL_Summary')

userWorkspace = path.dirname(path.dirname(helc_gdb))
projectName = path.basename(userWorkspace)
textFilePath = path.join(userWorkspace, f"{projectName}_log.txt")

### Geodatabase Validation and Cleanup ###
if not Exists(helc_gdb):
    AddMsgAndPrint('\Failed to locate the project HELC.gdb', 2)
    exit()

if not Exists(support_gdb):
    AddMsgAndPrint('\nSUPPORT.gdb does not exist in the same path as HEL Tools', 2)
    exit()

if not Exists(scratch_gdb):
    CreateFileGDB(base_dir, 'scratch.gdb')

output_layers = [fieldDetermination, helSummary, lidarHEL, finalHELSummary]
deleteScratchLayers(output_layers)

# Remove output layers from map - Handles case when different sites run in same APRX
output_layer_names = ['Field_Determination', 'Initial_HEL_Summary', 'Final_HEL_Summary', 'LiDAR_HEL_Summary']
removeMapLayers(map, output_layer_names)

### ESRI Environment Settings ###
env.scratchWorkspace = scratch_gdb
env.overwriteOutput = True


### HEL Determination Procedure ###
try:
    # Stamp CLU into field determination fc. Exit if no CLU fields selected
    CopyFeatures(cluLayer, fieldDetermination)

    # Make sure tract_number and farm_number  are unique; exit otherwise
    uniqueTracts = list(set([row[0] for row in SearchCursor(fieldDetermination, ('tract_number'))]))
    uniqueFarm   = list(set([row[0] for row in SearchCursor(fieldDetermination, ('farm_number'))]))

    if len(uniqueTracts) != 1:
        AddMsgAndPrint(f"\n\tThere are {str(len(uniqueTracts))} different Tract Numbers. Exiting!", 2)
        for tract in uniqueTracts:
            AddMsgAndPrint(f"\t\tTract #: {str(tract)}", 2)
        exit()

    if len(uniqueFarm) != 1:
        AddMsgAndPrint(f"\n\tThere are {str(len(uniqueFarm))} different Farm Numbers. Exiting!", 2)
        for farm in uniqueFarm:
            AddMsgAndPrint(f"\t\tFarm #: {str(farm)}", 2)
        exit()

    # Start logging to text file
    logBasicSettings(textFilePath, helLayer, inputDEM, zUnits, use_runoff_ls)

    # Add Calcacre field if it doesn't exist. Should be part of the CLU layer.
    calcAcreFld = 'clu_calculated_acres'
    if not len(ListFields(fieldDetermination, calcAcreFld)) > 0:
        AddField(fieldDetermination, calcAcreFld, 'DOUBLE')

    # Note: FSA MIDAS uses "square meters * 0.0002471" based on NAD 83 for the current UTM Zone and then rounds to two decimal points to set its calc acres.
    # If we changed all calc acres formulas to match FSA's formula, we would have matching FSA acres, but slightly incorrect amounts.
    # The variance is approximately two hundred thouandths of an acre or about 3/10ths of a square inch per acre.
    # If we set all internal acres computations to 2 decimal places from rounding based on the above, all acres would be consistent, except possibly for raster derived acres (need to check).
    CalculateField(fieldDetermination, calcAcreFld, '!shape.area@acres!', 'PYTHON_9.3')
    totalAcres = float('%.1f' % (sum([row[0] for row in SearchCursor(fieldDetermination, (calcAcreFld))])))
    AddMsgAndPrint(f"\nTotal Acres: {str(totalAcres)}", textFilePath=textFilePath)

    # Z-factor conversion Lookup table
    # lookup dictionary to convert XY units to area. Key = XY unit of DEM; Value = conversion factor to sq.meters
    acreConversionDict = {'Meter':4046.8564224, 'Foot':43560, 'Foot_US':43560, 'Centimeter':40470000, 'Inch':6273000}

    # Assign Z-factor based on XY and Z units of DEM
    # the following represents a matrix of possible z-Factors
    # using different combination of xy and z units
    # ----------------------------------------------------
    #                      Z - Units
    #                       Meter    Foot     Centimeter     Inch
    #          Meter         1	    0.3048	    0.01	    0.0254
    #  XY      Foot        3.28084	  1	      0.0328084	    0.083333
    # Units    Centimeter   100	    30.48	     1	         2.54
    #          Inch        39.3701	  12       0.393701	      1
    # ---------------------------------------------------

    unitLookUpDict = {'Meter':0, 'Meters':0, 'Foot':1, 'Foot_US':1, 'Feet':1, 'Centimeter':2, 'Centimeters':2, 'Inch':3, 'Inches':3}
    zFactorList = [[1,0.3048,0.01,0.0254], [3.28084,1,0.0328084,0.083333], [100,30.48,1,2.54], [39.3701,12,0.393701,1]]

    # Compute Summary of original HEL values
    # Intersect fieldDetermination (CLU & AOI) with soils (helLayer) -> finalHELSummary
    SetProgressorLabel('Computing summary of original HEL Values...')
    AddMsgAndPrint('\nComputing summary of original HEL Values:', textFilePath=textFilePath)
    cluHELintersect_pre = path.join('in_memory', path.basename(CreateScratchName('cluHELintersect_pre', data_type='FeatureClass', workspace=scratch_gdb)))

    # Use the catalog path of the hel layer to avoid using a selection
    helLayerPath = Describe(helLayer).catalogPath

    # Intersect fieldDetermination with soils and explode into single part
    Intersect([fieldDetermination, helLayerPath], cluHELintersect_pre, 'ALL')
    MultipartToSinglepart(cluHELintersect_pre, finalHELSummary)
    scratchLayers.append(cluHELintersect_pre)

    # Test intersection --- Should we check the percentage of intersection here? what if only 50% overlap
    # TODO: Explore better method for intersection check, Count Overlap?
    # No modification needed for these acres. The total is used only for this check.
    totalIntAcres = sum([row[0] for row in SearchCursor(finalHELSummary, ('SHAPE@AREA'))]) / acreConversionDict.get(Describe(finalHELSummary).SpatialReference.LinearUnitName)
    if not totalIntAcres:
        AddMsgAndPrint('\tThere is no overlap between HEL soil layer and CLU Layer. Exiting!', 2, textFilePath)
        exit()

    # Dissolve intersection output by the following fields -> helSummary
    cluNumberFld = 'clu_number'
    dissovleFlds = [cluNumberFld, 'tract_number', 'farm_number', 'county_code', 'clu_calculated_acres', hel_field]

    # Dissolve the finalHELSummary to report input summary
    Dissolve(finalHELSummary, helSummary, dissovleFlds, '', 'MULTI_PART', 'DISSOLVE_LINES')

    # Add and Update fields in the HEL Summary Layer (Og_HELcode, Og_HEL_Acres, Og_HEL_AcrePct)
    # Add 3 fields to the intersected layer. The intersected 'clueHELintersect' layer will be used for the dissolve process and at the end of the script.
    HELrasterCode = 'Og_HELcode'    # Used for rasterization purposes
    HELacres = 'Og_HEL_Acres'
    HELacrePct = 'Og_HEL_AcrePct'

    if not len(ListFields(helSummary, HELrasterCode)) > 0:
        AddField(helSummary, HELrasterCode, 'SHORT')

    if not len(ListFields(helSummary, HELacres)) > 0:
        AddField(helSummary, HELacres, 'DOUBLE')

    if not len(ListFields(helSummary, HELacrePct)) > 0:
        AddField(helSummary, HELacrePct, 'DOUBLE')

    # Calculate HELValue Field
    helSummaryDict = dict()     ## tallies acres by HEL value i.e. {PHEL:100}
    nullHEL = 0                 ## # of polygons with no HEL values
    wrongHELvalues = list()     ## Stores incorrect HEL Values
    maxAcreLength = list()      ## Stores the number of acre digits for formatting purposes
    bNoPHELvalues = False       ## Boolean flag to indicate PHEL values are missing

    # HEL Field, Og_HELcode, Og_HEL_Acres, Og_HEL_AcrePct, "SHAPE@AREA", "clu_calculated_acres"
    with UpdateCursor(helSummary, [hel_field, HELrasterCode, HELacres, HELacrePct, 'SHAPE@AREA', calcAcreFld]) as cursor:
        for row in cursor:
            # Update HEL value field; Continue if NULL HEL value
            if row[0] is None or row[0] == '' or len(row[0]) == 0:
                nullHEL += 1
                continue
            elif row[0] == 'HEL':
                row[1] = 0
            elif row[0] == 'NHEL':
                row[1] = 1
            elif row[0] == 'PHEL':
                row[1] = 2
            elif row[0] == 'NA':
                row[1] = 1
            else:
                if not str(row[0]) in wrongHELvalues:
                    wrongHELvalues.append(str(row[0]))

            # Update Acre field
            # Here we calculated acres differently than we did than when we updated the calc acres in the field determination layer. Seems like we could be consistent here.
            # Differences may be inconsequential if our decimal places match ArcMap's and everything is consistent for coordinate systems for the layers.
            #acres = float('%.1f' % (row[3] / acreConversionDict.get(Describe(helSummary).SpatialReference.LinearUnitName)))
            acres = row[4] / acreConversionDict.get(Describe(helSummary).SpatialReference.LinearUnitName)
            row[2] = acres
            maxAcreLength.append(float('%.1f' %(acres)))

            # Update Pct field
            pct = float('%.2f' %((row[2] / row[5]) * 100)) # HEL acre percentage
            if pct > 100.0: pct = 100.0                    # set pct to 100 if its greater; rounding issue
            row[3] = pct

            # Add hel value to dictionary to summarize by total project
            if row[0] not in helSummaryDict:
                helSummaryDict[row[0]] = acres
            else:
                helSummaryDict[row[0]] += acres

            cursor.updateRow(row)

    # No PHEL values were found; Bypass geoprocessing and populate form
    if 'PHEL' not in helSummaryDict:
        bNoPHELvalues = True

    # Inform user about NULL values; Exit if any NULLs exist.
    if nullHEL > 0:
        AddMsgAndPrint(f"\n\tERROR: There are {str(nullHEL)} polygon(s) with missing HEL values. Exiting!", 2, textFilePath)
        exit()

    # Inform user about invalid HEL values (not PHEL,HEL, NHEL); Exit if invalid values exist.
    if wrongHELvalues:
        AddMsgAndPrint(f"\n\tERROR: There is {str(len(set(wrongHELvalues)))} invalid HEL values in HEL Layer:", 2, textFilePath)
        for wrongVal in set(wrongHELvalues):
            AddMsgAndPrint(f"\t\t{wrongVal}", 2, textFilePath)
        exit()

    # Report HEl Layer Summary by field
    AddMsgAndPrint('\n\tSummary by CLU:', textFilePath=textFilePath)

    # Create 2 temporary tables to capture summary statistics
    ogHelSummaryStats = path.join('in_memory', path.basename(CreateScratchName('ogHELSummaryStats', data_type='ArcInfoTable', workspace=scratch_gdb)))
    ogHelSummaryStatsPivot = path.join('in_memory', path.basename(CreateScratchName('ogHELSummaryStatsPivot', data_type='ArcInfoTable', workspace=scratch_gdb)))

    stats = [[HELacres, 'SUM']]
    caseField = [cluNumberFld, hel_field]
    Statistics(helSummary, ogHelSummaryStats, stats, caseField)
    sumHELacreFld = [fld.name for fld in ListFields(ogHelSummaryStats, '*' + HELacres)][0]
    scratchLayers.append(ogHelSummaryStats)

    # Pivot table will have clu_number & any HEL values present (HEL,NHEL,PHEL)
    PivotTable(ogHelSummaryStats, cluNumberFld, hel_field, sumHELacreFld, ogHelSummaryStatsPivot)
    scratchLayers.append(ogHelSummaryStatsPivot)

    pivotFields = [fld.name for fld in ListFields(ogHelSummaryStatsPivot)][1:]  # ['clu_number','HEL','NHEL','PHEL']
    numOfhelValues = len(pivotFields)                                                 # Number of Pivot table fields; Min 2 fields
    maxAcreLength.sort(reverse=True)
    bSkipGeoprocessing = True             # Skip processing until a field is neither HEL >= 33.33% or NHEL > 66.67%

    # Change any nulls to 0 in the pivot table
    fieldList = [fld.name for fld in ListFields(ogHelSummaryStatsPivot)]
    cursor = UpdateCursor(ogHelSummaryStatsPivot, fieldList)
    for row in cursor:
        index = 0
        while index <= numOfhelValues:
            if row[index] == None:
                row[index] = 0
                index += 1
            else:
                index += 1
        cursor.updateRow(row)

    # This dictionary will only be used if FINAL results are all HEL or all NHEL to reference original
    # acres and not use tabulate area acres.  It will also be used when there are no PHEL Values.
    # {cluNumber:(HEL value, cluAcres, HEL Pct} -- HEL value is determined by the 33.33% or 50 acre rule
    ogCLUinfoDict = dict()

    # Iterate through the pivot table and report HEL values by CLU - ['clu_number','HEL','NHEL','PHEL']
    with SearchCursor(ogHelSummaryStatsPivot, pivotFields) as cursor:
        for row in cursor:
            og_cluHELrating = None         # original field HEL Rating
            og_cluHELacresList = list()    # temp list of acres by HEL value
            og_cluHELpctList = list()      # temp list of pct by HEL value
            msgList = list()               # temp list of messages to print
            cluAcres = sum([row[i] for i in range(1, numOfhelValues, 1)])
            # strictly to determine if geoprocessing is needed
            bHELgreaterthan33 = False
            bNHELgreaterthan66 = False

            # iterate through the pivot table fields by record
            for i in range(1, numOfhelValues, 1):
                acres =  float('%.1f' % (row[i]))
                pct = float('%.1f' % ((row[i] / cluAcres) * 100))

                # set pct to 100 if its greater; rounding issue
                if pct > 100.0: pct = 100.0

                # Determine HEL rating of original fields and populate acres
                # and pc into ogCLUinfoDict.  Primarily for bNoPHELvalues.
                # Also determine if further geoProcessing is needed.
                if og_cluHELrating == None:

                    # Set field to HEL
                    if pivotFields[i] == 'HEL' and (pct >= 33.33 or acres >= 50):
                        og_cluHELrating = 'HEL'
                        if not row[0] in ogCLUinfoDict:
                            ogCLUinfoDict[row[0]] = (og_cluHELrating, cluAcres, pct)
                        bHELgreaterthan33 = True

                    # Set field to NHEL
                    elif pivotFields[i] == 'NHEL' and pct > 66.67:
                        bNHELgreaterthan66 = True
                        og_cluHELrating = 'NHEL'
                        if not row[0] in ogCLUinfoDict:
                            ogCLUinfoDict[row[0]] = (og_cluHELrating, cluAcres, pct)

                    # This is the last field in the pivot table
                    elif i == (numOfhelValues - 1):
                        og_cluHELrating = pivotFields[i]
                        if not row[0] in ogCLUinfoDict:
                            ogCLUinfoDict[row[0]] = (og_cluHELrating, cluAcres, pct)

                    # First field did not meet HEL criteria; add it to a temp list
                    else:
                        og_cluHELacresList.append(row[i])
                        og_cluHELpctList.append(pct)

                # Formulate messages but don't print yet
                firstSpace = ' ' * (4-len(pivotFields[i]))                                    # PHEL has 4 characters
                secondSpace = ' ' * (len(str(maxAcreLength[0])) - len(str(acres)))            # Number of spaces
                msgList.append(str(f"\t\t\t{pivotFields[i]}{firstSpace} -- {str(acres)}{secondSpace} .ac -- {str(pct)} %"))

            # Skip geoprocessing if HEL >=33.33% or NHEL > 66.67%
            if bSkipGeoprocessing:
                if not bHELgreaterthan33 and not bNHELgreaterthan66:
                    bSkipGeoprocessing = False

            # Report messages to user; og CLU HEL rating will be reported if bNoPHELvalues is true.
            if bNoPHELvalues:
                AddMsgAndPrint(f"\n\t\tCLU #: {str(row[0])} - Rating: {og_cluHELrating}", textFilePath=textFilePath)
            else:
                AddMsgAndPrint(f"\n\t\tCLU #: {str(row[0])}", textFilePath=textFilePath)
            for msg in msgList:
                AddMsgAndPrint(msg, textFilePath=textFilePath)


    # No PHEL Values Found
    if bNoPHELvalues or bSkipGeoprocessing:
        if bNoPHELvalues:
            AddMsgAndPrint('\n\tThere are no PHEL values in HEL layer...', 1, textFilePath)
            AddMsgAndPrint('\tNo Geoprocessing is required...\n', 1, textFilePath)

        # Only Print this if there are PHEL values but they don't need
        # to be processed; Otherwise it should be captured by above statement.
        if bSkipGeoprocessing and not bNoPHELvalues:
            AddMsgAndPrint('\n\tHEL values are >= 33.33% or more than 50 acres, or NHEL values are > 66.67%', 1, textFilePath)
            AddMsgAndPrint('\tNo Geoprocessing is required...\n', 1, textFilePath)

        # Add 3 fields to fieldDetermination layer
        fieldList = ['HEL_YES', 'HEL_Acres', 'HEL_Pct']
        for field in fieldList:
            if not len(ListFields(fieldDetermination, field)) > 0:
                if field == 'HEL_YES':
                    AddField(fieldDetermination, field, 'TEXT', '', '', 5)
                else:
                    AddField(fieldDetermination, field, 'FLOAT')
        fieldList.append(cluNumberFld)

        # Update new fields using ogCLUinfoDict
        with UpdateCursor(fieldDetermination, fieldList) as cursor:
            for row in cursor:
                row[0] = ogCLUinfoDict.get(row[3])[0]   # "HEL_YES" value
                row[1] = ogCLUinfoDict.get(row[3])[1]   # "HEL_Acres" value
                row[2] = ogCLUinfoDict.get(row[3])[2]   # "HEL_Pct" value
                cursor.updateRow(row)

        # Add 4 fields to Final HEL Summary layer
        newFields = ['Polygon_Acres', 'Final_HEL_Value', 'Final_HEL_Acres', 'Final_HEL_Percent']
        for fld in newFields:
            if not len(ListFields(finalHELSummary, fld)) > 0:
                if fld == 'Final_HEL_Value':
                    AddField(finalHELSummary, 'Final_HEL_Value', 'TEXT', '', '', 5)
                else:
                    AddField(finalHELSummary, fld, 'DOUBLE')
        newFields.append(hel_field)
        newFields.append(cluNumberFld)
        newFields.append('SHAPE@AREA')

        # [polyAcres,finalHELvalue,finalHELacres,finalHELpct,MUHELCL,'CLUNBR',"SHAPE@AREA"]
        with UpdateCursor(finalHELSummary, newFields) as cursor:
            for row in cursor:
                # Calculate polygon acres;
                # TODO: change to GIS calc acres, remove dict
                row[0] = row[6] / acreConversionDict.get(Describe(finalHELSummary).SpatialReference.LinearUnitName)
                # Final_HEL_Value will be set to the initial HEL value
                row[1] = row[4]
                # set Final HEL Acres to 0 for PHEL and NHEL; othewise set to polyAcres
                if row[4] in ('NHEL', 'PHEL'):
                    row[2] = 0.0
                else:
                    row[2] = row[0]
                # Calculate percent of polygon relative to CLU
                cluAcres = ogCLUinfoDict.get(row[5])[1]
                pct = (row[0] / cluAcres) * 100
                if pct > 100.0: pct = 100.0
                row[3] = pct
                cursor.updateRow(row)

        # Add output layers to map and clear Site Prepare selection
        SetProgressorLabel('Adding output layers to map...')
        AddMsgAndPrint('\nAdding output layers to map...', textFilePath=textFilePath)
        lyr_name_list = [lyr.longName for lyr in map.listLayers()]
        addLyrxByConnectionProperties(map, lyr_name_list, lidar_hel_summary_lyrx, helc_gdb, visible=False)
        addLyrxByConnectionProperties(map, lyr_name_list, initial_hel_summary_lyrx, helc_gdb, visible=False)
        addLyrxByConnectionProperties(map, lyr_name_list, final_hel_summary_lyrx, helc_gdb, visible=False)
        addLyrxByConnectionProperties(map, lyr_name_list, field_determination_lyrx, helc_gdb)
        cluLayer.setSelectionSet(method='NEW')
        map.listLayers('Site_Prepare_HELC')[0].visible = False
        
        # Gracefully exit script when no geoprocessing is required
        raise(NoProcesingExit)


    # Check and create DEM clip from buffered CLU
    # Exit if a DEM is not present; At this point PHEL mapunits are present and requires a DEM to process them.
    try:
        Describe(inputDEM).baseName
    except:
        AddMsgAndPrint('\nDEM is required to process PHEL values. Exiting!', 2, textFilePath)
        exit()

    units, zFactor, dem = extractDEM(cluLayer, inputDEM, fieldDetermination, scratch_gdb, zFactorList, unitLookUpDict, zUnits)
    if not zFactor or not dem:
        exit()
    
    scratchLayers.append(dem)

    # Check DEM for NoData overlaps with input CLU fields
    AddMsgAndPrint('\nChecking input DEM for site coverage...', textFilePath=textFilePath)
    vectorNull = path.join('in_memory', path.basename(CreateScratchName('vectorNull', data_type='FeatureClass', workspace=scratch_gdb)))
    demCheck = path.join('in_memory', path.basename(CreateScratchName('demCheck', data_type='FeatureClass', workspace=scratch_gdb)))

    # Use Set Null statement to change things with value of 0 to NoData
    whereClause = 'VALUE = 0'
    setNull = SetNull(dem, dem, whereClause)

    # Use IsNull to convert NoData values in the DEM to 1 and all other values to 0
    demNull = IsNull(setNull)

    # Convert the IsNull raster to a vector layer
    try:
        RasterToPolygon(demNull, vectorNull, 'SIMPLIFY', 'Value', 'MULTIPLE_OUTER_PART')
    except:
        RasterToPolygon(demNull, vectorNull, 'SIMPLIFY', 'Value')

    scratchLayers.append(vectorNull)
    Delete(setNull)
    Delete(demNull)

    # Clip the IsNull vector layer by the field determination layer
    Clip(vectorNull, fieldDetermination, demCheck)
    scratchLayers.append(demCheck)

    # Search for any values of 1 in the demCheck layer and issue a warning to the user if present
    fields = ['gridcode']
    cursor = SearchCursor(demCheck, fields)
    nd_warning = False
    for row in cursor:
        if row[0] == 1:
            nd_warning = True

    # If no data warning is True, show error messages
    if nd_warning == True:
        AddMsgAndPrint('\nThe input DEM may have null data within the input CLU fields. Please review the \ninput DEM for coverage of the site, as well as the results layers, to determine \nif they are reasonable to use for this determination. If the DEM is insufficient \nfor the site, this determination should be made onsite. \n\nA DEM with a few missing pixels is usually sufficient, but a DEM with large null areas is not.', 1, textFilePath)
    else:
        AddMsgAndPrint('\nDEM values in site extent are not null. Continuing...', textFilePath=textFilePath)

    # Create Slope Layer
    # Perform a minor fill to reduce LiDAR data noise and minor irregularities. Try to use a max fill height of no more than 1 foot, based on input zUnits.
    SetProgressorLabel('Filling small sinks in DEM...')
    AddMsgAndPrint('\nFilling small sinks in DEM...', textFilePath=textFilePath)
    if zUnits == 'Feet':
        zLimit = 1
    elif zUnits == 'Meters':
        zLimit = 0.3048
    elif zUnits == 'Inches':
        zLimit = 12
    elif zUnits == 'Centimeters':
        zLimit = 30.48
    else:
        # Assume worst case z units of Meters
        zLimit = 0.3048

    # 1 Perform the fill using the zLimit as the max fill amount
    filled = Fill(dem, zLimit)
    scratchLayers.append(filled)

    # 2 Run a FocalMean to smooth the DEM of LiDAR data noise. This should be run prior to creating derivative products.
    # This replaces running FocalMean on the slope layer itself.
    SetProgressorLabel('Running Focal Statistics on DEM...')
    AddMsgAndPrint('\nRunning Focal Statistics on DEM...', textFilePath=textFilePath)
    preslope = FocalStatistics(filled, NbrRectangle(3, 3, 'CELL'), 'MEAN', 'DATA')

    # 3 Create Slope
    SetProgressorLabel('Creating Slope Derivative...')
    AddMsgAndPrint('\nCreating Slope Derivative...', textFilePath=textFilePath)
    slope = Slope(preslope, 'PERCENT_RISE', zFactor)

    # 4 Create Flow Direction and Flow Length
    SetProgressorLabel('Calculating Flow Direction...')
    AddMsgAndPrint('\nCalculating Flow Direction...', textFilePath=textFilePath)
    flowDirection = FlowDirection(preslope, 'FORCE')
    scratchLayers.append(flowDirection)

    # 5 Calculate Flow Length
    SetProgressorLabel('Calculating Flow Length...')
    AddMsgAndPrint('\nCalculating Flow Length...', textFilePath=textFilePath)
    preflowLength = FlowLength(flowDirection, 'UPSTREAM', '')
    scratchLayers.append(preflowLength)

    # 6 Run a focal statistics on flow length
    SetProgressorLabel('Running Focal Statistics on Flow Length...')
    AddMsgAndPrint('\nRunning Focal Statistics on Flow Length...', textFilePath=textFilePath)
    flowLength = FocalStatistics(preflowLength, NbrRectangle(3, 3, 'CELL'), 'MAXIMUM', 'DATA')
    scratchLayers.append(flowLength)

    # 7 convert Flow Length distance units to feet if original DEM LINEAR UNITS ARE not in feet.
    if not units in ('Feet', 'Foot', 'Foot_US'):
        AddMsgAndPrint('\nConverting Flow Length distance units to feet...', textFilePath=textFilePath)
        flowLengthFT = flowLength * 3.280839896
        scratchLayers.append(flowLengthFT)
    else:
        flowLengthFT = flowLength
        scratchLayers.append(flowLengthFT)

    # 8 Convert slope percent to radians for use in various LS equations
    radians = ATan(Times(slope, 0.01))

    # Compute LS Factor
    # If Northwest US 'Use Runoff LS Equation' flag was active, use the following equation
    if use_runoff_ls:
        SetProgressorLabel('Calculating LS Factor...')
        AddMsgAndPrint('\nCalculating LS Factor...', textFilePath=textFilePath)
        lsFactor = (Power((flowLengthFT/72.6)*Cos(radians),0.5))*(Power(Sin((radians))/(Sin(5.143*((pi)/180))),0.7))

    # Otherwise, use the standard AH537 LS computation
    else:
        # 9 Calculate S Factor
        SetProgressorLabel('Calculating S Factor...')
        AddMsgAndPrint('\nCalculating S Factor...', textFilePath=textFilePath)
        # Compute S factor using formula in AH537, pg 12
        sFactor = ((Power(Sin(radians),2)*65.41)+(Sin(radians)*4.56)+(0.065))
        scratchLayers.append(sFactor)

        # 10 Calculate L Factor
        SetProgressorLabel('Calculating L Factor...')
        AddMsgAndPrint('\nCalculating L Factor...', textFilePath=textFilePath)

        # Original outlFactor lines
        """outlFactor = Con(Raster(slope),Power(Raster(flowLengthFT) / 72.6,0.2),
                            Con(Raster(slope),Power(Raster(flowLengthFT) / 72.6,0.3),
                            Con(Raster(slope),Power(Raster(flowLengthFT) / 72.6,0.4),
                            Power(Raster(flowLengthFT) / 72.6,0.5),"VALUE >= 3 AND VALUE < 5"),"VALUE >= 1 AND VALUE < 3"),"VALUE<1")"""

        # Remove 'Raster' function from above
        lFactor = Con(slope,Power(flowLengthFT / 72.6,0.2),
                        Con(slope,Power(flowLengthFT / 72.6,0.3),
                        Con(slope,Power(flowLengthFT / 72.6,0.4),
                        Power(flowLengthFT / 72.6,0.5), 'VALUE >= 3 AND VALUE < 5'), 'VALUE >= 1 AND VALUE < 3'), 'VALUE < 1')

        scratchLayers.append(lFactor)

        # 11 Calculate LS Factor "%l_factor%" * "%s_factor%"
        SetProgressorLabel('Calculating LS Factor...')
        AddMsgAndPrint('\nCalculating LS Factor...', textFilePath=textFilePath)
        lsFactor = lFactor * sFactor

    scratchLayers.append(radians)
    scratchLayers.append(lsFactor)

    # Convert K,T & R Factor and HEL Value to Rasters
    AddMsgAndPrint('\nConverting Vector to Raster for Spatial Analysis...', textFilePath=textFilePath)
    cellSize = Describe(dem).MeanCellWidth

    # This works in 10.5 AND works in 10.6.1 and 10.7 but slows processing
    kFactor = CreateScratchName('kFactor', data_type='RasterDataset', workspace=scratch_gdb)
    tFactor = CreateScratchName('tFactor', data_type='RasterDataset', workspace=scratch_gdb)
    rFactor = CreateScratchName('rFactor', data_type='RasterDataset', workspace=scratch_gdb)
    helValue = CreateScratchName('helValue', data_type='RasterDataset', workspace=scratch_gdb)

    # 12 Convert KFactor to raster
    SetProgressorLabel('Converting K Factor field to a raster...')
    AddMsgAndPrint('\tConverting K Factor field to a raster...', textFilePath=textFilePath)
    FeatureToRaster(finalHELSummary, k_field, kFactor, cellSize)

    # 13 Convert TFactor to raster
    SetProgressorLabel('Converting T Factor field to a raster...')
    AddMsgAndPrint('\tConverting T Factor field to a raster...', textFilePath=textFilePath)
    FeatureToRaster(finalHELSummary, t_field, tFactor, cellSize)

    # 14 Convert RFactor to raster
    SetProgressorLabel('Converting R Factor field to a raster...')
    AddMsgAndPrint('\tConverting R Factor field to a raster...', textFilePath=textFilePath)
    FeatureToRaster(finalHELSummary, r_field, rFactor, cellSize)

    SetProgressorLabel('Converting HEL Value field to a raster...')
    AddMsgAndPrint('\tConverting HEL Value field to a raster...', textFilePath=textFilePath)
    FeatureToRaster(helSummary, HELrasterCode, helValue, cellSize)

    scratchLayers.append(kFactor)
    scratchLayers.append(tFactor)
    scratchLayers.append(rFactor)
    scratchLayers.append(helValue)

    # Calculate EI Factor
    SetProgressorLabel('Calculating EI Factor...')
    AddMsgAndPrint('\nCalculating EI Factor...', textFilePath=textFilePath)
    eiFactor = Divide((lsFactor * kFactor * rFactor), tFactor)
    scratchLayers.append(eiFactor)

    # Calculate Final HEL Factor
    # Create Conditional statement to reflect the following:
    # 1) PHEL Value = 0 -- Take EI factor -- Depends     2
    # 2) HEL Value  = 1 -- Assign 9                      0
    # 3) NHEL Value = 2 -- Assign 2 (No action needed)   1
    # Anything above 8 is HEL

    SetProgressorLabel('Calculating HEL Factor...')
    AddMsgAndPrint('\nCalculating HEL Factor...', textFilePath=textFilePath)
    helFactor = Con(helValue, eiFactor, Con(helValue, 9, helValue, 'VALUE = 0'), 'VALUE = 2')
    scratchLayers.append(helFactor)

    # Reclassify values:
    #       < 8 = Value_1 = NHEL
    #       > 8 = Value_2 = HEL
    remapString = '0 8 1;8 100000000 2'
    Reclassify_3d(helFactor, 'VALUE', remapString, lidarHEL, 'NODATA')

    # Determine if individual PHEL delineations are HEL/NHEL"""
    SetProgressorLabel('Computing summary of LiDAR HEL Values...')
    AddMsgAndPrint('\nComputing summary of LiDAR HEL Values:\n', textFilePath=textFilePath)

    # Summarize new values between HEL soil polygon and lidarHEL raster
    outPolyTabulate = path.join('in_memory', path.basename(CreateScratchName('HEL_Polygon_Tabulate', data_type='ArcInfoTable', workspace=scratch_gdb)))
    zoneFld = Describe(finalHELSummary).OIDFieldName
    TabulateArea(finalHELSummary, zoneFld, lidarHEL, 'VALUE', outPolyTabulate, cellSize)
    tabulateFields = [fld.name for fld in ListFields(outPolyTabulate)][2:]
    scratchLayers.append(outPolyTabulate)

    # Add 4 fields to Final HEL Summary layer
    newFields = ['Polygon_Acres', 'Final_HEL_Value', 'Final_HEL_Acres', 'Final_HEL_Percent']
    for fld in newFields:
        if not len(ListFields(finalHELSummary,fld)) > 0:
            if fld == 'Final_HEL_Value':
                AddField(finalHELSummary, 'Final_HEL_Value', 'TEXT', '', '', 5)
            else:
                AddField(finalHELSummary, fld, 'DOUBLE')

    # In some cases, the finalHELSummary layer's OID field name was "OBJECTID_1" which
    # conflicted with the output of the tabulate area table.
    if zoneFld.find('_') > -1:
        outputJoinFld = zoneFld
    else:
        outputJoinFld = f"{zoneFld}_1"
    JoinField(finalHELSummary, zoneFld, outPolyTabulate, outputJoinFld, tabulateFields)

    # Booleans to indicate if only HEL or only NHEL is present
    bOnlyHEL = False; bOnlyNHEL = False

    # Check if VALUE_1(NHEL) or VALUE_2(HEL) are missing from outPolyTabulate table
    finalHELSummaryFlds = [fld.name for fld in ListFields(finalHELSummary)][2:]
    if len(finalHELSummaryFlds):

        # NHEL is not Present - so All is HEL; All is VALUE2
        if not 'VALUE_1' in tabulateFields:
            AddMsgAndPrint('\tWARNING: Entire Area is HEL', 1, textFilePath)
            AddField(finalHELSummary, 'VALUE_1', 'DOUBLE')
            CalculateField(finalHELSummary, 'VALUE_1', 0)
            bOnlyHEL = True

        # HEL is not Present - All is NHEL; All is VALUE1
        if not 'VALUE_2' in tabulateFields:
            AddMsgAndPrint('\tWARNING: Entire Area is NHEL', 1, textFilePath)
            AddField(finalHELSummary, 'VALUE_2', 'DOUBLE')
            CalculateField(finalHELSummary, 'VALUE_2', 0)
            bOnlyNHEL = True
    else:
        AddMsgAndPrint('\n\tReclassifying helFactor failed. Exiting!', 2, textFilePath)
        exit()

    newFields.append('VALUE_2')
    newFields.append('SHAPE@AREA')
    newFields.append(cluNumberFld)

    # this will be used for field determination
    fieldDeterminationDict = dict()

    # [polyAcres,finalHELvalue,finalHELacres,finalHELpct,"VALUE_2","SHAPE@AREA","clu_number"]
    with UpdateCursor(finalHELSummary, newFields) as cursor:
        for row in cursor:
            # Calculate polygon acres
            row[0] = row[5] / acreConversionDict.get(Describe(finalHELSummary).SpatialReference.LinearUnitName)
            # Convert "VALUE_2" values to acres.  Represent acres from a poly that is HEL.
            # The intersection of CLU and soils may cause slivers below the tabulate cell size
            # which will create NULLs.  Set these slivers to 0 acres.
            try:
                row[2] = row[4] / acreConversionDict.get(Describe(finalHELSummary).SpatialReference.LinearUnitName)
            except:
                row[2] = 0

            # Calculate percentage of the polygon that is HEL
            row[3] = (row[2] / row[0]) * 100

            # set pct to 100 if its greater; rounding issue
            if row[3] > 100.0: row[3] = 100.0

            # polygon HEL Pct is greater than 50%; HEL
            if row[3] > 50.0:
                row[1] = 'HEL'
                # Add the HEL polygon acres to the dict
                if not row[6] in fieldDeterminationDict:
                    fieldDeterminationDict[row[6]] = row[0]
                else:
                    fieldDeterminationDict[row[6]] += row[0]

            # polygon HEL Pct is less than 50%; NHEL
            else:
                row[1] = 'NHEL'
                # Don't Add NHEL polygon acres to dict but place
                # holder for the clu
                if not row[6] in fieldDeterminationDict:
                    fieldDeterminationDict[row[6]] = 0

            cursor.updateRow(row)

    # Delete unwanted fields from the finalHELSummary Layer
    newFields.remove('VALUE_2')
    validFlds = [cluNumberFld, 'state_code', 'tract_number', 'farm_number', 'county_code', 'clu_calculated_acres', hel_field, musym_field, muname_field, muwat_field, muwnd_field] + newFields

    deleteFlds = list()
    for fld in [f.name for f in ListFields(finalHELSummary)]:
        if fld in (zoneFld, 'Shape_Area', 'Shape_Length', 'Shape'):continue
        if not fld in validFlds:
            deleteFlds.append(fld)

    DeleteField(finalHELSummary, deleteFlds)

    # Determine if field is HEL/NHEL. Add 3 fields to fieldDetermination layer
    fieldList = ['HEL_YES', 'HEL_Acres', 'HEL_Pct']
    for field in fieldList:
        if not len(ListFields(fieldDetermination, field)) > 0:
            if field == 'HEL_YES':
                AddField(fieldDetermination, field, 'TEXT', '', '', 5)
            else:
                AddField(fieldDetermination, field, 'FLOAT')

    fieldList.append(cluNumberFld)
    fieldList.append(calcAcreFld)
    cluDict = dict()  # Strictly for formatting; clu_number: (len of clu, helAcres, helPct, len of Acres, len of pct,is it HEL?)

    # ['HEL_YES','HEL_Acres','HEL_Pct','clu_number','clu_calculated_acres']
    with UpdateCursor(fieldDetermination, fieldList) as cursor:
        for row in cursor:
            # if results are completely HEL or NHEL then get total clu acres from ogCLUinfoDict
            if bOnlyHEL or bOnlyNHEL:
                if bOnlyHEL:
                    helAcres = ogCLUinfoDict.get(row[3])[1]
                    nhelAcres = 0.0
                    helPct = 100.0
                    nhelPct = 0.0
                else:
                    nhelAcres = ogCLUinfoDict.get(row[3])[1]
                    helAcres = 0.0
                    helPct = 0.0
                    nhelPct = 100.0
            else:
                helAcres = fieldDeterminationDict[row[3]]    # total HEL acres for field
                helPct = (helAcres / row[4]) * 100           # helAcres / clu_calculated_acres
                nhelAcres = row[4] - helAcres
                nhelPct = 100 - helPct
                # set pct to 100 if its greater; rounding issue
                if helPct > 100.0: helPct = 100.0
                if nhelPct > 100.0: nhelPct = 100.0

            clu = row[3]

            if helPct >= 33.33 or helAcres > 50.0:
                row[0] = 'HEL'
            else:
                row[0] = 'NHEL'

            row[1] = helAcres
            row[2] = helPct

            helAcres = float('%.1f' %(helAcres))   # Strictly for formatting
            helPct = float('%.1f' %(helPct))       # Strictly for formatting
            nhelAcres = float('%.1f' %(nhelAcres)) # Strictly for formatting
            nhelPct = float('%.1f' %(nhelPct))     # Strictly for formatting
            # {8: (25.3, 4, 45.1, 30.8, 4, 54.9, 'HEL')}
            cluDict[clu] = (helAcres, len(str(helAcres)), helPct, nhelAcres, len(str(nhelAcres)), nhelPct, row[0])

            cursor.updateRow(row)


    # Strictly for formatting and printing
    maxHelAcreLength = sorted([cluinfo[1] for clu, cluinfo in cluDict.items()], reverse=True)[0]
    maxNHelAcreLength = sorted([cluinfo[4] for clu, cluinfo in cluDict.items()], reverse=True)[0]

    for clu in sorted(cluDict.keys()):
        firstSpace = ' '  * (maxHelAcreLength - cluDict[clu][1])
        secondSpace = ' ' * (maxNHelAcreLength - cluDict[clu][4])
        helAcres = cluDict[clu][0]
        helPct = cluDict[clu][2]
        nHelAcres = cluDict[clu][3]
        nHelPct = cluDict[clu][5]
        yesOrNo = cluDict[clu][6]
        AddMsgAndPrint(f"\tCLU #: {str(clu)}", textFilePath=textFilePath)
        AddMsgAndPrint(f"\t\tHEL Acres:  {str(helAcres)}{firstSpace} .ac -- {str(helPct)} %", textFilePath=textFilePath)
        AddMsgAndPrint(f"\t\tNHEL Acres: {str(nHelAcres)}{secondSpace} .ac -- {str(nHelPct)} %", textFilePath=textFilePath)
        AddMsgAndPrint(f"\t\tHEL Determination: {yesOrNo}\n", textFilePath=textFilePath)


    # Add output layers to map and symbolize
    SetProgressorLabel('Adding output layers to map...')
    AddMsgAndPrint('Adding output layers to map...', textFilePath=textFilePath)
    lyr_name_list = [lyr.longName for lyr in map.listLayers()]
    addLyrxByConnectionProperties(map, lyr_name_list, lidar_hel_summary_lyrx, helc_gdb, visible=False)
    addLyrxByConnectionProperties(map, lyr_name_list, initial_hel_summary_lyrx, helc_gdb, visible=False)
    addLyrxByConnectionProperties(map, lyr_name_list, final_hel_summary_lyrx, helc_gdb, visible=False)
    addLyrxByConnectionProperties(map, lyr_name_list, field_determination_lyrx, helc_gdb)
    cluLayer.setSelectionSet(method='NEW')
    map.listLayers('Site_Prepare_HELC')[0].visible = False


except NoProcesingExit:
    pass

except:
    try:
        AddMsgAndPrint(errorMsg('HEL Determination'), 2, textFilePath)
    except:
        AddMsgAndPrint(errorMsg('HEL Determination'), 2)

finally:
    SetProgressorLabel('Cleaning up scratch layers...')
    AddMsgAndPrint('\nCleaning up scratch layers...')
    deleteScratchLayers(scratchLayers)
