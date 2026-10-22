Write a Python script that uses a Google Drive Python client and the specification of EcoTaxa's API available from https://ecotaxa.obs-vlfr.fr/api/docs.

As configuration variables, at the begining of the script if should have:
- google_drive_folder: the URL for a Google Drive folder
- ecotaxa_project_id: the numeric id of the EcoTaxa project
and the credentials, stored in a secure manner, for access to Google Drive and EcoTaxa (login, password and/or token). The authentication should be fully automated after the first time: the script is meant to run as an unattended job.

The script should:
- list the samples available from EcoTaxa and get their original sample id (orig_id) with https://ecotaxa.obs-vlfr.fr/api/docs#/samples/samples_search
- list the files matching ecotaxa_*.zip in the Google Drive, where * would be the sample orig_id
- download the files not already on EcoTaxa
- upload the .zip files using the TUS protocole, taking inspiration from the code at https://github.com/ecotaxa/CytoProcess/blob/main/cytoprocess/ecotaxa.py#L242
- import the uploaded files into the EcoTaxa project, taking inspiration from https://github.com/ecotaxa/CytoProcess/blob/main/cytoprocess/ecotaxa.py#L369
- run a prediction using https://ecotaxa.obs-vlfr.fr/api/docs#/objects/predict_object_set for all non-validated objects, using project 21194 as source_project_ids, all used categories, all features, learning_limit=5000

