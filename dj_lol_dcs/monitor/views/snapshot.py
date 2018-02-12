from django.http import FileResponse, HttpResponseServerError
from django.conf import settings

import pexpect
import os
import time


def generate_database_dump(request):

    # Remove any existing database dumps older than 10min
    tmp_folder = os.path.join(settings.BASE_DIR, 'tmp')
    if not os.path.exists(tmp_folder):
        return HttpResponseServerError('A /tmp directory must be created, with ownership of the WSGI daemon\'s server')
    try:
        for filename in os.listdir(tmp_folder):
            last_modified = os.path.join(tmp_folder, filename)
            if last_modified < (time.time() - 60*10):
                os.remove(os.path.join(tmp_folder, filename))

        # Make sure not to override ongoing (another) request from before (within 10min)
        tmp_file_location = os.path.join(tmp_folder, 'dcs_dump.sql.zip')
        variant = 1
        exists = os.path.exists(tmp_file_location)
        while exists:
            tmp_file_location = os.path.join(settings.BASE_DIR, 'tmp', 'dcs_dump_{}.sql.zip'.format(variant))
            exists = os.path.exists(tmp_file_location)
            variant += 1

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
        child_process.wait()  # Wait for pg_dump process to finish; Blocking
    except PermissionError:
        return HttpResponseServerError('The tmp directory must be owned by the WSGI daemon\'s server')

    return FileResponse(open(tmp_file_location, 'rb'), streaming_content='application/octet-stream')
