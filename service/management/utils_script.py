from .. import app
from .. import normalizer

from elasticsearch import helpers
from .elasticsearch_params import DEFAULT_SETTINGS
from .elasticsearch_mappings import MAP_STATE
from .elasticsearch_mappings import MAP_DEPT
from .elasticsearch_mappings import MAP_MUNI
from .elasticsearch_mappings import MAP_SETTLEMENT
from .elasticsearch_mappings import MAP_STREET
from . import download

import psycopg2
import argparse
import os
import urllib.parse
import json
import smtplib
import logging
import uuid
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from io import StringIO

loggerStream = StringIO()
logger = logging.getLogger(__name__)

# Versión de archivos del ETL compatibles con ésta versión de API.
# Modificar su valor cuando se haya actualizdo el código para tomar
# nuevas versiones de los archivos.
FILE_VERSION = '2.0.0'

SEPARATOR_WIDTH = 60
ACTIONS = ['index', 'index_stats', 'run_sql']
TIMEOUT = 500


def setup_logger(l, loggerStream):
    l.setLevel(logging.INFO)

    stdoutHandler = logging.StreamHandler()
    stdoutHandler.setLevel(logging.INFO)

    strHandler = logging.StreamHandler(loggerStream)
    strHandler.setLevel(logging.INFO)

    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s',
                                  '%Y-%m-%d %H:%M:%S')
    stdoutHandler.setFormatter(formatter)
    strHandler.setFormatter(formatter)

    l.addHandler(stdoutHandler)
    l.addHandler(strHandler)


def send_email(host, user, password, subject, message, recipients,
               attachments=None):
    with smtplib.SMTP_SSL(host) as smtp:
        smtp.login(user, password)

        msg = MIMEMultipart()
        msg['Subject'] = subject
        msg["From"] = user
        msg["To"] = ",".join(recipients)
        msg.attach(MIMEText(message))

        for name, contents in (attachments or {}).items():
            attachment = MIMEText(contents)
            attachment['Content-Disposition'] = \
                'attachment; filename="{}"'.format(name)
            msg.attach(attachment)

        smtp.send_message(msg)


def print_log_separator(l, message):
    l.info("=" * SEPARATOR_WIDTH)
    l.info("|" + " " * (SEPARATOR_WIDTH - 2) + "|")

    l.info("|" + message.center(SEPARATOR_WIDTH - 2) + "|")

    l.info("|" + " " * (SEPARATOR_WIDTH - 2) + "|")
    l.info("=" * SEPARATOR_WIDTH)


class GeorefIndex:
    def __init__(self, alias, filepath, backup_filepath, mapping,
                 excludes=None, docs_key='entidades'):
        self.alias = alias
        self.docs_key = docs_key
        self.filepath = filepath
        self.backup_filepath = backup_filepath
        self.mapping = mapping
        self.excludes = excludes or []

    def fetch_data(self, filepath):
        data = None

        if urllib.parse.urlparse(filepath).scheme in ['http', 'https']:
            logger.info('Descargando archivo:')
            logger.info(' + {}'.format(filepath))
            logger.info('')

            try:
                content = download.download(filepath)
                data = json.loads(content.decode())
            except Exception:
                logger.warning('No se pudo descargar el archivo.')
                logger.warning('')
        else:
            logger.info('Accediendo al archivo:')
            logger.info(' + {}'.format(filepath))
            logger.info('')

            try:
                with open(filepath) as f:
                    data = json.load(f)
            except Exception:
                logger.warning('No se pudo acceder al archivo JSON.')
                logger.warning('')

        return data

    def create_or_reindex(self, es, forced=False):
        print_log_separator(logger,
                            'Creando/reindexando {}'.format(self.alias))
        logger.info('')

        data = self.fetch_data(self.filepath)
        ok = self.create_or_reindex_with_data(es, data,
                                              check_timestamp=not forced)

        if forced and not ok:
            logger.warning('No se pudo indexar utilizando fuente primaria.')
            logger.warning('Intentando nuevamente con backup...')
            logger.warning('')

            data = self.fetch_data(self.backup_filepath)
            ok = self.create_or_reindex_with_data(es, data,
                                                  check_timestamp=False)

            if not ok:
                # TODO: Agregar manejo de errores adicional
                logger.error('No se pudo indexar utilizando backups.')
                logger.error('')

        if ok:
            self.write_backup(data)

    def create_or_reindex_with_data(self, es, data, check_timestamp=True):
        if not data:
            logger.warning('No existen datos a indexar.')
            return False

        timestamp = data['timestamp']
        version = data['version']
        docs = data[self.docs_key]

        logger.info('Versión de API:   {}'.format(FILE_VERSION))
        logger.info('Versión de Datos: {}'.format(version))
        logger.info('')

        if version.split('.')[0] != FILE_VERSION.split('.')[0]:
            logger.warning('Salteando creación de nuevo índice:')
            logger.warning('Versiones de datos no compatibles.')
            logger.info('')
            return False

        new_index = '{}-{}-{}'.format(self.alias,
                                      uuid.uuid4().hex[:8], timestamp)
        old_index = self.get_old_index(es)

        if check_timestamp:
            if not self.check_index_newer(new_index, old_index):
                logger.warning(
                    'Salteando creación de índice {}'.format(new_index))
                logger.warning(
                    (' + El índice {} ya existente es idéntico o más' +
                     ' reciente').format(old_index))
                logger.info('')
                return False
        else:
            logger.info('Omitiendo chequeo de timestamp.')
            logger.info('')

        self.create_index(es, new_index)
        self.insert_documents(es, new_index, docs)

        self.update_aliases(es, new_index, old_index)
        if old_index:
            self.delete_index(es, old_index)

        return True

    def write_backup(self, data):
        logger.info('Creando archivo de backup...')
        with open(self.backup_filepath, 'w') as f:
            json.dump(data, f)
        logger.info('Archivo creado.')
        logger.info('')

    def create_index(self, es, index):
        logger.info('Creando nuevo índice: {}...'.format(index))
        logger.info('')
        es.indices.create(index=index, body={
            'settings': DEFAULT_SETTINGS,
            'mappings': self.mapping
        })

    def insert_documents(self, es, index, docs):
        operations = self.bulk_update_generator(docs, index)
        creations, errors = 0, 0

        logger.info('Insertando documentos...')

        for ok, response in helpers.streaming_bulk(es, operations,
                                                   raise_on_error=False,
                                                   request_timeout=TIMEOUT):
            if ok and response['create']['result'] == 'created':
                creations += 1
            else:
                errors += 1
                identifier = response['create']['_id']
                error = response['create']['error']

                logger.warning(
                    'Error al procesar el documento ID {}:'.format(identifier))
                logger.warning(json.dumps(error, indent=4, ensure_ascii=False))
                logger.warning('')

        logger.info('Resumen:')
        logger.info(' + Documentos procesados: {}'.format(len(docs)))
        logger.info(' + Documentos creados: {}'.format(creations))
        logger.info(' + Errores: {}'.format(errors))
        logger.info('')

    def delete_index(self, es, old_index):
        logger.info('Eliminando índice anterior ({})...'.format(old_index))
        es.indices.delete(old_index)
        logger.info('Índice eliminado.')
        logger.info('')

    def update_aliases(self, es, index, old_index):
        logger.info('Actualizando aliases...')

        alias_ops = []
        if old_index:
            alias_ops.append({
                'remove': {
                    'index': old_index,
                    'alias': self.alias
                }
            })

        alias_ops.append({
            'add': {
                'index': index,
                'alias': self.alias
            }
        })

        logger.info('Existen {} operaciones de alias.'.format(len(alias_ops)))

        for op in alias_ops:
            if 'add' in op:
                logger.info(' + Agregar {} como alias de {}'.format(
                    op['add']['alias'], op['add']['index']))
            else:
                logger.info(' + Remover {} como alias de {}'.format(
                    op['remove']['alias'], op['remove']['index']))

        es.indices.update_aliases({'actions': alias_ops})

        logger.info('')
        logger.info('Aliases actualizados.')
        logger.info('')

    def check_index_newer(self, new_index, old_index):
        if not old_index:
            return True

        new_date = datetime.fromtimestamp(int(new_index.split('-')[-1]))
        old_date = datetime.fromtimestamp(int(old_index.split('-')[-1]))

        return new_date > old_date

    def get_old_index(self, es):
        if not es.indices.exists_alias(name=self.alias):
            return None
        return list(es.indices.get_alias(name=self.alias).keys())[0]

    def filter_doc(self, doc):
        return {
            key: doc[key]
            for key in doc
            if key not in self.excludes
        }

    def bulk_update_generator(self, docs, index):
        """Crea un generador de operaciones 'create' para Elasticsearch a
        partir de una lista de documentos a indexar.

        Args:
            docs (list): Documentos a indexar.
            index (str): Nombre del índice.

        """
        for original_doc in docs:
            doc = self.filter_doc(original_doc)

            action = {
                '_op_type': 'create',
                '_type': '_doc',
                '_id': doc['id'],
                '_index': index,
                '_source': doc
            }

            yield action


def send_index_email(config, forced, env, log):
    lines = log.splitlines()
    warnings = len([line for line in lines if 'WARNING' in line])
    errors = len([line for line in lines if 'ERROR' in line])

    subject = 'Georef API [{}] Index - Errores: {} - Warnings: {}'.format(
        env,
        errors,
        warnings
    )
    msg = 'Indexación de datos para Georef API. Modo forzado: {}'.format(
        forced)

    send_email(config['host'], config['user'], config['password'], subject,
               msg, config['recipients'], {
                   'log.txt': log
               })


def run_index(app, es, forced):
    backups_dir = app.config['BACKUPS_DIR']
    os.makedirs(backups_dir, exist_ok=True)

    env = app.config['GEOREF_ENV']
    logger.info('Comenzando (re)indexación en Georef API [{}]'.format(env))
    logger.info('')

    indices = [
        GeorefIndex(alias='provincias',
                    filepath=app.config['STATES_FILE'],
                    backup_filepath=os.path.join(backups_dir,
                                                 'provincias.json'),
                    mapping=MAP_STATE),
        GeorefIndex(alias='departamentos',
                    filepath=app.config['DEPARTMENTS_FILE'],
                    backup_filepath=os.path.join(backups_dir,
                                                 'departamentos.json'),
                    mapping=MAP_DEPT),
        GeorefIndex(alias='municipios',
                    filepath=app.config['MUNICIPALITIES_FILE'],
                    backup_filepath=os.path.join(backups_dir,
                                                 'municipios.json'),
                    mapping=MAP_MUNI),
        GeorefIndex(alias='localidades',
                    filepath=app.config['LOCALITIES_FILE'],
                    backup_filepath=os.path.join(backups_dir,
                                                 'localidades.json'),
                    mapping=MAP_SETTLEMENT),
        GeorefIndex(alias='calles',
                    filepath=app.config['STREETS_FILE'],
                    backup_filepath=os.path.join(backups_dir, 'calles.json'),
                    mapping=MAP_STREET,
                    excludes=['codigo_postal'],
                    docs_key='vias')
    ]

    for index in indices:
        try:
            index.create_or_reindex(es, forced)
        except Exception as e:
            logger.error('')
            logger.exception('Ocurrió un error al indexar:')
            logger.error('')

    logger.info('')

    mail_config = app.config.get_namespace('EMAIL_')
    if mail_config['enabled']:
        logger.info('Enviando mail...')
        send_index_email(mail_config, forced, env,
                         loggerStream.getvalue())
        logger.info('Mail enviado.')


def run_info(es):
    logger.info('INDICES:')
    for line in es.cat.indices(v=True).splitlines():
        logger.info(line)
    logger.info('')

    logger.info('ALIASES:')
    for line in es.cat.aliases(v=True).splitlines():
        logger.info(line)
    logger.info('')

    logger.info('NODES:')
    for line in es.cat.nodes(v=True).splitlines():
        logger.info(line)


def run_sql(app, script):
    try:
        conn = psycopg2.connect(host=app.config['SQL_DB_HOST'],
                                dbname=app.config['SQL_DB_NAME'],
                                user=app.config['SQL_DB_USER'],
                                password=app.config['SQL_DB_PASS'])

        sql = script.read()
        with conn:
            with conn.cursor() as cursor:
                cursor.execute(sql)

        conn.close()
        logger.info('El script SQL fue ejecutado correctamente.')
    except psycopg2.Error as e:
        logger.error('Ocurrió un error al ejecutar el script SQL:')
        logger.error(e)


def main():
    print(os.getcwd())
    parser = argparse.ArgumentParser()
    parser.add_argument('-m', '--mode', metavar='<action>', required=True,
                        choices=ACTIONS)
    parser.add_argument('-s', '--script', metavar='<path>',
                        type=argparse.FileType())
    parser.add_argument('-f', '--forced', action='store_true')
    parser.add_argument('-i', '--info', action='store_true',
                        help='Mostrar información de índices y salir.')
    args = parser.parse_args()

    setup_logger(logger, loggerStream)

    with app.app_context():
        if args.mode in ['index', 'index_stats']:
            es = normalizer.get_elasticsearch()

            if args.mode == 'index':
                run_index(app, es, args.forced)
            else:
                run_info(es)

        elif args.mode == 'run_sql':
            run_sql(app, args.script)


if __name__ == '__main__':
    main()
