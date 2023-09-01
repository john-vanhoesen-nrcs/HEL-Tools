from getpass import getuser
from os import path
from sys import argv, exit
from time import ctime

from arcpy import Describe, env, Exists, GetParameterAsText, SetProgressorLabel
from arcpy.conversion import TableToTable
from arcpy.da import InsertCursor, SearchCursor, UpdateCursor
from arcpy.management import Compact, CreateFeatureDataset, CreateFileGDB, CreateTable, Delete, DeleteRows, GetCount
from arcpy.mp import ArcGISProject

from hel_utils import AddMsgAndPrint, errorMsg, getLayout, updateLayoutText


# ================================================================================================================
def logBasicSettings():
    f = open(textFilePath, 'a+')
    f.write('\n######################################################################\n')
    f.write('Executing \"Enter Project Info\" tool...\n')
    f.write(f"User Name: {getuser()}\n")
    f.write(f"Date Executed: {ctime()}\n")
    f.write('User Parameters:\n')
    f.write(f"\tSelected CLU Layer: {sourceCLU}\n")
    f.write(f"\tClient Name: {client}\n")
    f.write(f"\tDelineator Name: {delineator}\n")
    f.write(f"\tDigitizer Name: {digitizer}\n")
    f.write(f"\tRequest Type: {requestType}\n")
    f.write(f"\tRequest Date: {requestDate}\n")
    f.close
    del f


# ================================================================================================================
# Set arcpy Environment Settings
env.overwriteOutput = True

# Check for active map in Pro Project
try:
    aprx = ArcGISProject('CURRENT')
    m = aprx.listMaps('HEL Determination')[0]
except:
    AddMsgAndPrint('\nThis tool must be run from an active ArcGIS Pro project that was developed from the template distributed with this toolbox. Exiting...\n', 2)
    exit()

# Main procedures
try:
    SetProgressorLabel('Reading inputs...')
    sourceCLU = GetParameterAsText(0)         # User selected CLU file from the project
    client = GetParameterAsText(1)            # Client Name
    delineator = GetParameterAsText(2)        # The person who conducts the technical determination
    digitizer = GetParameterAsText(3)         # The person who digitizes the determination (may or may not match the delineator)
    requestType = GetParameterAsText(4)       # Request Type (AD-1026, FSA-569, or NRCS-CPA-38)
    requestDate = GetParameterAsText(5)       # Determination request date per request form signature date
    clientStreet = GetParameterAsText(6)
    clientStreet2 = GetParameterAsText(7)
    clientCity = GetParameterAsText(8)
    clientState = GetParameterAsText(9)
    clientZip = GetParameterAsText(10)

    # Get the basedataGDB_path from the input CLU layer. If else retained in case of other project path oddities.
    sourceCLU_path = Describe(sourceCLU).CatalogPath
    if sourceCLU_path.find('.gdb') > 0 and sourceCLU_path.find('Determinations') > 0 and sourceCLU_path.find('Site_CLU') > 0:
        basedataGDB_path = sourceCLU_path[:sourceCLU_path.find('.gdb')+4]
    else:
        AddMsgAndPrint('\nSelected CLU layer is not from a Determinations project folder. Exiting...', 2)
        exit()

    # Define Variables
    SetProgressorLabel('Setting variables...')
    basedataGDB_name = path.basename(basedataGDB_path)
    userWorkspace = path.dirname(basedataGDB_path)
    projectName = path.basename(userWorkspace).replace(' ', '_')
    cluName = 'Site_CLU'
    projectCLU = path.join(basedataGDB_path, 'Layers', cluName)
    # daoiName = 'Site_Define_AOI' #TODO: Do we need this layer for HEL?
    helDir = path.join(userWorkspace, 'HEL')
    
    helGDB_name = f"{path.basename(userWorkspace).replace(' ', '_')}_HELC.gdb"
    helGDB_path = path.join(helDir, helGDB_name)
    helFD = path.join(helGDB_path, 'HELC_Data')
    
    templateTable = path.join(path.dirname(argv[0]), path.join('SUPPORT.gdb', 'table_admin'))
    tableName = f"Table_{projectName}"

    # Permanent Datasets
    projectTable = path.join(basedataGDB_path, tableName)
    helDetTable = path.join(helGDB_path, 'Admin_Table')

    # Set up log file path and start logging
    SetProgressorLabel('Starting log file...')
    textFilePath = path.join(userWorkspace, f"{projectName}_log.txt")
    logBasicSettings()

    # Get Job ID from input CLU
    SetProgressorLabel('Recording project Job ID...')
    fields = ['job_id']
    with SearchCursor(projectCLU, fields) as cursor:
        for row in cursor:
            jobid = row[0]
            break

    # Determine if the job's admin table does not exist, else create the table.
    if not Exists(projectTable):
        AddMsgAndPrint('\nCreating administrative table...')
        SetProgressorLabel('Creating administrative table...')
        CreateTable(basedataGDB_path, tableName, templateTable)

    # Get count of the records in the table, delete all rows
    SetProgressorLabel('Updating project table...')
    recordsCount = int(GetCount(projectTable)[0])
    if recordsCount > 1:
        DeleteRows(projectTable)

    # If record count is zero, create a new row
    if recordsCount == 0:
        with InsertCursor(projectTable, ['*']) as cursor:
            cursor.insertRow()

    # Update entries to the row in the table. This tool always overwrites
    # Use a search cursor to get the tract location info from the CLU layer
    AddMsgAndPrint('\nImporting tract data from the CLU...')
    SetProgressorLabel('Importing tract data from the CLU...')
    field_names = ['admin_state','admin_state_name','admin_county','admin_county_name',
                   'state_code','state_name','county_code','county_name','farm_number','tract_number']
    with SearchCursor(sourceCLU, field_names) as cursor:
        for row in cursor:
            adminState = row[0]
            adminStateName = row[1]
            adminCounty = row[2]
            adminCountyName = row[3]
            stateCode = row[4]
            stateName = row[5]
            countyCode = row[6]
            countyName = row[7]
            farmNumber = row[8]
            tractNumber = row[9]
            break

    # Use an update cursor to update all values in the admin table at once. Always overwrite.
    AddMsgAndPrint('\nUpdating the administrative table...')
    SetProgressorLabel('Updating the administrative table...')
    field_names = ['admin_state','admin_state_name','admin_county','admin_county_name','state_code','state_name',
                   'county_code','county_name','farm_number','tract_number','client','deter_staff',
                   'dig_staff','request_date','request_type','street','street_2','city','state','zip','job_id']
    with UpdateCursor(projectTable, field_names) as cursor:
        for row in cursor:
            row[0] = adminState
            row[1] = adminStateName
            row[2] = adminCounty
            row[3] = adminCountyName
            row[4] = stateCode
            row[5] = stateName
            row[6] = countyCode
            row[7] = countyName
            row[8] = farmNumber
            row[9] = tractNumber
            row[10] = client
            row[11] = delineator
            row[12] = digitizer
            row[13] = requestDate
            row[14] = requestType
            if clientStreet != '':
                row[15] = clientStreet
            if clientStreet2 != '':
                row[16] = clientStreet2
            if clientCity != '':
                row[17] = clientCity
            if clientState != '':
                row[18] = clientState
            if clientZip != '':
                row[19] = clientZip
            row[20] = jobid
            cursor.updateRow(row)

    # Create a text file output version of the admin table for consumption by external data collection forms
    # Set a file name and export to the user workspace folder for the project
    AddMsgAndPrint('\nExporting administrative text file...')
    SetProgressorLabel('Exporting administrative text file...')
    textTable = f"Admin_Info_{projectName}.txt"
    if Exists(textTable):
        Delete(textTable)
    TableToTable(projectTable, userWorkspace, textTable)

    # Update template map layouts in the project
    AddMsgAndPrint('\nUpdating map layouts...')
    SetProgressorLabel('Updating map layouts...')
    
    # Define the map layouts
    HEL_layout = getLayout(aprx, 'HEL Determination Layout')
    
    # Update the map layouts
    if HEL_layout:
        updateLayoutText(HEL_layout, farmNumber, tractNumber, countyName, adminCountyName, client)

    # If project HEL geodatabase and feature dataset do not exist, create them.
    # Get the spatial reference from the Define AOI feature class and use it, if needed
    AddMsgAndPrint('\nChecking project integrity...')
    SetProgressorLabel('Checking project integrity...')
    desc = Describe(sourceCLU)
    sr = desc.SpatialReference
    
    if not Exists(helGDB_path):
        AddMsgAndPrint('\tCreating HEL geodatabase...')
        SetProgressorLabel('Creating HEL geodatabase...')
        CreateFileGDB(helDir, helGDB_name)

    if not Exists(helFD):
        AddMsgAndPrint('\tCreating HEL feature dataset...')
        SetProgressorLabel('Creating HEL feature dataset...')
        CreateFeatureDataset(helGDB_path, 'HELC_Data', sr)

    # Copy the administrative table into the wetlands database for use with the attribute rules during digitizing
    AddMsgAndPrint('\nUpdating administrative table in GDB...')
    if Exists(helDetTable):
        Delete(helDetTable)
    TableToTable(projectTable, helGDB_path, 'Admin_Table')

    # Adjust layer visibility in maps, turn off CLU layer
    AddMsgAndPrint('\nUpdating layer visibility to off...')
    off_names = [cluName]
    for maps in aprx.listMaps():
        for lyr in maps.listLayers():
            for name in off_names:
                if name in lyr.longName:
                    lyr.visible = False

    # Turn on DAOI layer
    # on_names = [daoiName]
    # for maps in aprx.listMaps():
    #     for lyr in maps.listLayers():
    #         for name in on_names:
    #             if name in lyr.longName:
    #                 lyr.visible = True

    # Compact file geodatabase
    try:
        AddMsgAndPrint('\nCompacting File Geodatabase...')
        SetProgressorLabel('Compacting File Geodatabase...')
        Compact(basedataGDB_path)
        AddMsgAndPrint('\tDone')
    except:
        pass
    
except SystemExit:
    pass

except KeyboardInterrupt:
    AddMsgAndPrint('Keyboard interruption requested... Exiting')

except:
    errorMsg()
