from django.http import FileResponse, HttpResponseServerError, HttpResponse, HttpResponseNotFound
from django.conf import settings

import pexpect
import os
import time


def create_database_dump(request):
    """
        Returns HTTP status:
        - 500 if configuration error (with explanation)
        - 201 if dumped already (within past 10min)
        - 200 if started creating dumping database successfully
    """

    # Check tmp folder exists, and ensure correct permissions using try-catch
    tmp_folder = os.path.join(settings.BASE_DIR, 'tmp')
    if not os.path.exists(tmp_folder):
        return HttpResponseServerError('A /tmp directory must be created, with ownership of the WSGI daemon\'s server')
    try:
        # Remove any existing database dump older than 15min
        tmp_file_location = os.path.join(tmp_folder, 'dcs_dump.sql.zip')
        if os.path.exists(tmp_file_location):
            last_modified = os.path.getmtime(tmp_file_location)
            if last_modified < (time.time() - 60*10):
                os.remove(tmp_file_location)

        # Check if the database dump already exists (=wasn't older than 15min)
        if os.path.exists(tmp_file_location):
            return HttpResponse('File already exists', status=201)

        # Dump database
        child_process = pexpect.spawn('pg_dump', ['--username={}'.format(os.environ['DJ_PG_USERNAME']),
                                                  '--host=localhost',
                                                  '--password',
                                                  '--clean',
                                                  '--create',
                                                  '--format=c',
                                                  '--file={}'.format(tmp_file_location),
                                                  os.environ['DJ_PG_DBNAME']])
        child_process.expect('Password:')
        child_process.sendline(os.environ['DJ_PG_PASSWORD'])
    except PermissionError:
        return HttpResponseServerError('The tmp directory must be owned by the WSGI daemon\'s server')
    return HttpResponse('Dumping database')  # status=200


def retrieve_database_dump(request):
    tmp_file_location = os.path.join(settings.BASE_DIR, 'tmp', 'dcs_dump.sql.zip')
    if os.path.exists(tmp_file_location):
        last_modified = os.path.getmtime(tmp_file_location)
        if last_modified < (time.time() - 60*10):
            return HttpResponseNotFound('Must dump database before retrieval (dumped database too long ago)')
    else:
        return HttpResponseNotFound('Must dump database before retrieval (none existing)')
    return FileResponse(open(tmp_file_location, 'rb'), streaming_content='application/octet-stream')
