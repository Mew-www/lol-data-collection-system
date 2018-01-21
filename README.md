# Setting up python with virtualenv in target system
sudo apt-get install python3  
sudo apt-get install python3-dev  
sudo apt-get install python-tk  
sudo apt-get install pip  
sudo pip install virtualenv  

# Backend persistence software
sudo apt-get install memcached  
sudo apt-get install postgresql  
sudo apt-get install libmariadbclient-dev  
sudo apt-get install mysql-server

# Configuring postgresqld (CHECK VERSION AFTER /etc/postgresql/, MAY VARY)
sudo nano /etc/postgresql/9.6/main/pg_hba.conf
-> change line "local all all peer" to "local all all password"  
-> [CTRL]+X -> Y -> [ENTER] save  
sudo -u postgres createuser --pwprompt --unencrypted dj_lol_dcs_user  
-> type password  
sudo -u postgres createdb --encoding=UTF8 --owner=dj_lol_dcs_user dj_lol_dcs_db  
-> save 'DJ_PG_USERNAME' in environment variables (VARIES PER SYSTEM) django finds it there  
-> save 'DJ_PG_PASSWORD' in environment variables (VARIES PER SYSTEM) django finds it there  
-> save 'DJ_PG_DBNAME' in environment variables (VARIES PER SYSTEM) django finds it there

# Configuring MySQL
-> set root password on installation  
-> open prompt and create the database and user  
-> SQL:  
--> CREATE DATABASE <dbname>;  
--> GRANT ALL PRIVILEGES ON <dbname>.* TO '<username>'@'localhost' IDENTIFIED BY '<password>';  
-> save 'MYSQL_REQUESTHISTORY_USERNAME' in environment variables (VARIES PER SYSTEM)  
-> save 'MYSQL_REQUESTHISTORY_PASSWORD' in environment variables (VARIES PER SYSTEM)  
-> save 'MYSQL_REQUESTHISTORY_DBNAME' in environment variables (VARIES PER SYSTEM)

# Configuring RIOT API
-> save 'RIOT_API_KEY' in environment variables (VARIES PER SYSTEM) both django and scripts find it there

# Loading 3rd party modules to project
cd lol-data-collection-system  
virtualenv -p python3 env  
source env/bin/activate  
pip install -r virtualenv_requirements.txt  

# Generating Django's secret key
python -c "import string,random; uni=string.ascii_letters+string.digits+string.punctuation; print repr(''.join([random.SystemRandom().choice(uni) for i in range(random.randint(45,50))]))"  
-> save 'DJ_SECRET_KEY' in environment variables (VARIES PER SYSTEM) django finds it there  

# (in test) Development-server
python manage.py runserver  

# (in production) Out-facing www-server
sudo apt-get install apache2  

# (in production) Configure apache2
sudo nano /etc/apache2/envvars  
-> save all used environment variables (DJ_PG_USERNAME, -PASSWORD, -DBNAME, DJ_SECRET_KEY) -> same syntax as .bashrc (APPEND TO EXISTING envvars FILE CONTENT)  
sudo apt-get install libapache2-mod-wsgi-py3  
sudo a2enmod wsgi  
sudo nano /etc/apache2/apache2.conf  
-> #COMMENT-OUT the default "Include sites-enabled" which has a :80 VHost already,   
-> then APPEND following content  
-> NOTICE THE PYTHON VERSION (IT MAY BE 3.5 OR 3.(whatever-subversion) BUT MUST BE A VALID PATH)  
-> ALSO NOTE THE (USER) FOLDER, MAY BE /opt/ (or /whatever/) ASWELL IF YOU PREFER  
&lt;VirtualHost *:80&gt;  
&nbsp;&nbsp;&nbsp;&nbsp;&lt;Directory /home/mew/lol-data-collection-system/dj_lol_dcs&gt;  
&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;Require all granted  
&nbsp;&nbsp;&nbsp;&nbsp;&lt;/Directory&gt;  
&nbsp;&nbsp;&nbsp;&nbsp;Alias /dcs/static /home/mew/lol-data-collection-system/dj_lol_dcs/static  
&nbsp;&nbsp;&nbsp;&nbsp;WSGIDaemonProcess dj_lol_dcs_wsgi python-path=/home/mew/lol-data-collection-system/env/lib/python3.5/site-packages:/home/mew/lol-data-collection-system/dj_lol_dcs  
&nbsp;&nbsp;&nbsp;&nbsp;WSGIProcessGroup dj_lol_dcs_wsgi  
&nbsp;&nbsp;&nbsp;&nbsp;WSGIScriptAlias /dcs /home/mew/lol-data-collection-system/dj_lol_dcs/dj_lol_dcs/wsgi.py process-group=dj_lol_dcs_wsgi  
&lt;/VirtualHost&gt;  
sudo service apache2 restart  

