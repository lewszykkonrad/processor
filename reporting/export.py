import logging
import json
import sys
import os
import cStringIO
import datetime as dt
import numpy as np
import pandas as pd
from sqlalchemy import create_engine
import expcolumns as exc

log = logging.getLogger()
config_path = 'Config/'


class DBUpload(object):
    def __init__(self):
        self.db = None
        self.dbs = None
        self.dft = None
        self.table = None
        self.id_col = None
        self.name = None
        self.values = None

    def upload_to_db(self, db_file, schema_file, translation_file, data_file):
        self.db = DB(db_file)
        logging.info('Uploading ' + data_file + ' to ' + self.db.db)
        self.dbs = DBSchema(schema_file)
        self.dft = DFTranslation(translation_file, data_file)
        for table in self.dbs.table_list:
            self.upload_table_to_db(table)
        logging.info(data_file + ' successfully upload to ' + self.db.db)

    def upload_table_to_db(self, table):
        logging.info('Uploading table ' + table + ' to ' + self.db.db)
        cols = self.dbs.get_cols_for_export(table)
        ul_df = self.dft.slice_for_upload(cols)
        ul_df = self.add_ids_to_df(self.dbs.fk, ul_df)
        self.dbs.set_table(table)
        pk_config = {table: self.dbs.pk.keys() + self.dbs.pk.values()}
        self.set_id_info(table, pk_config, ul_df)
        if exc.upload_id_col in ul_df.columns:
            df_rds = self.read_rds_table(table, list(ul_df.columns),
                                         exc.upload_id_col,
                                         [self.dft.upload_id])
            df = pd.merge(df_rds, ul_df, how='outer', on=self.name,
                          indicator=True)
            df = df.drop_duplicates(self.name).reset_index()
            self.update_rows(df, df_rds.columns, table)
            self.delete_rows(df, table)
            self.insert_rows(df, table)
        else:
            df_rds = self.read_rds_table(table, list(ul_df.columns),
                                         self.name, self.values)
            df = pd.merge(df_rds, ul_df, how='outer', on=self.name,
                          indicator=True)
            df = df.drop_duplicates(self.name).reset_index()
            self.update_rows(df, df_rds.columns, table)
            self.insert_rows(df, table)

    def read_rds_table(self, table, cols, where_col, where_val):
        df_rds = self.db.read_rds_table(table, where_col, where_val)
        df_rds = df_rds[cols]
        df_rds = self.dft.clean_types_for_upload(df_rds)
        return df_rds

    def update_rows(self, df, cols, table):
        df_update = df[df['_merge'] == 'both']
        updated_index = []
        set_cols = [x for x in cols if x not in
                    [self.name, self.id_col, exc.upload_id_col]]
        for col in set_cols:
            df_changed = (df_update[df_update[col + '_y'] !=
                                    df_update[col + '_x']]
                          [[self.name, col + '_y']])
            updated_index.extend(df_changed.index)
        if updated_index:
            df_update = self.get_right_df(df_update)
            df_update = df_update.loc[updated_index]
            df_update = df_update[[self.name] + set_cols]
            set_vals = [tuple(x) for x in df_update.values]
            if exc.upload_id_col in df_update.columns:
                self.db.update_rows_two_where(table, set_cols, set_vals,
                                              self.name, exc.upload_id_col,
                                              self.dft.upload_id)
            else:
                self.db.update_rows(table, set_cols, set_vals, self.name)

    def delete_rows(self, df, table):
        df_delete = df[df['_merge'] == 'left_only']
        delete_vals = df_delete[self.name].tolist()
        if delete_vals:
            self.db.delete_rows(table, exc.upload_id_col, self.dft.upload_id,
                                self.name, delete_vals)

    def insert_rows(self, df, table):
        df_insert = df[df['_merge'] == 'right_only']
        df_insert = self.get_right_df(df_insert)
        if self.id_col in df_insert.columns:
            df_insert = df_insert.drop([self.id_col], axis=1)
        if not df_insert.empty:
            self.db.copy_from(table, df_insert, df_insert.columns)

    def get_right_df(self, df):
        cols = [x for x in df.columns if x[-2:] == '_y']
        df = df[[self.name] + cols]
        df.columns = ([self.name] + [x[:-2] for x in df.columns
                      if x[-2:] == '_y'])
        return df

    def add_ids_to_df(self, id_config, sliced_df):
        for id_table in id_config:
            if id_table == exc.upload_tbl:
                continue
            df_rds = self.format_and_read_rds(id_table, id_config, sliced_df)
            sliced_df = sliced_df.merge(df_rds, how='outer', on=self.name)
            sliced_df = sliced_df.drop(self.name, axis=1)
            sliced_df = self.dft.df_col_to_type(sliced_df, self.id_col, 'INT')
        return sliced_df

    def format_and_read_rds(self, table, id_config, sliced_df):
        self.set_id_info(table, id_config, sliced_df)
        self.dbs.set_table(table)
        if exc.upload_id_col in self.dbs.cols:
            df_rds = self.db.read_rds_two_where(table, self.id_col, self.name,
                                                self.values, exc.upload_id_col,
                                                self.dft.upload_id)
        else:
            df_rds = self.db.read_rds(table, self.id_col, self.name,
                                      self.values)
        return df_rds

    def set_id_info(self, table, id_config, sliced_df):
        self.table = table
        self.id_col = id_config[table][0]
        self.name = id_config[table][1]
        self.values = sliced_df[self.name].tolist()


# noinspection SqlResolve
class DB(object):
    def __init__(self, config):
        self.user = None
        self.pw = None
        self.host = None
        self.port = None
        self.db = None
        self.config_list = []
        self.configfile = None
        self.engine = None
        self.connection = None
        self.cursor = None
        self.output = None
        self.config = config
        self.input_config(self.config)
        self.conn_string = ('postgresql://{0}:{1}@{2}:{3}/{4}'.
                            format(*self.config_list))

    def input_config(self, config):
        logging.info('Loading DB config file: ' + str(config))
        self.configfile = config_path + config
        self.load_config()
        self.check_config()

    def load_config(self):
        try:
            with open(self.configfile, 'r') as f:
                self.config = json.load(f)
        except IOError:
            logging.error(self.configfile + ' not found.  Aborting.')
            sys.exit(0)
        self.user = self.config['USER']
        self.pw = self.config['PASS']
        self.host = self.config['HOST']
        self.port = self.config['PORT']
        self.db = self.config['DATABASE']
        self.config_list = [self.user, self.pw, self.host, self.port, self.db]

    def check_config(self):
        for item in self.config_list:
            if item == '':
                logging.warn(item + 'not in DB config file.  Aborting.')
                sys.exit(0)

    def connect(self):
        logging.debug('Connecting to DB at Host: ' + self.host)
        self.engine = create_engine(self.conn_string)
        self.connection = self.engine.raw_connection()
        self.cursor = self.connection.cursor()

    def df_to_output(self, df):
        self.output = cStringIO.StringIO()
        df.to_csv(self.output, sep='\t', header=False, index=False,
                  encoding='utf-8')
        self.output.seek(0)

    def copy_from(self, table, df, columns):
        table_path = self.db + '.' + table
        self.connect()
        logging.info('Writing ' + str(len(df)) + ' row(s) to ' + table)
        self.df_to_output(df)
        cur = self.connection.cursor()
        cur.copy_from(self.output, table=table_path, columns=columns)
        self.connection.commit()
        cur.close()

    def insert_rds(self, table, columns, values, return_col):
        self.connect()
        command = """
                  INSERT INTO {0}.{1} ({2})
                   VALUES ({3})
                   RETURNING ({4})
                  """.format(self.db, table, ', '.join(columns),
                             ', '.join(['%s'] * len(values)), return_col)
        self.cursor.execute(command, values)
        self.connection.commit()
        data = self.cursor.fetchall()
        data = pd.DataFrame(data=data, columns=[return_col])
        return data

    def delete_rows(self, table, where_col, where_val,
                    where_col2, where_vals2):
        logging.info('Deleting ' + str(len(where_vals2)) +
                     ' row(s) from ' + table)
        self.connect()
        command = """
                  DELETE FROM {0}.{1}
                   WHERE {0}.{1}.{2} IN ({3})
                   AND {0}.{1}.{4} IN ({5})
                  """.format(self.db, table, where_col, where_val, where_col2,
                             ', '.join(['%s'] * len(where_vals2)))
        self.cursor.execute(command, where_vals2)
        self.connection.commit()

    def read_rds_two_where(self, table, select_col, where_col, where_val,
                           where_col2, where_val2):
        self.connect()
        if select_col == where_col:
            command = """
                      SELECT {0}.{1}.{2}
                       FROM {0}.{1}
                       WHERE {0}.{1}.{3} IN ({4})
                       AND {0}.{1}.{5} IN ({6})
                      """.format(self.db, table, select_col, where_col,
                                 ', '.join(['%s'] * len(where_val)),
                                 where_col2, where_val2)
        else:
            command = """
                      SELECT {0}.{1}.{2}, {0}.{1}.{3}
                       FROM {0}.{1}
                       WHERE {0}.{1}.{3} IN ({4})
                       AND {0}.{1}.{5} IN ({6})
                      """.format(self.db, table, select_col, where_col,
                                 ', '.join(['%s'] * len(where_val)),
                                 where_col2, where_val2)
        self.cursor.execute(command, where_val)
        data = self.cursor.fetchall()
        if select_col == where_col:
            data = pd.DataFrame(data=data, columns=[select_col])
        else:
            data = pd.DataFrame(data=data, columns=[select_col, where_col])
        return data

    def read_rds(self, table, select_col, where_col, where_val):
        self.connect()
        if select_col == where_col:
            command = """
                      SELECT {0}.{1}.{2}
                       FROM {0}.{1}
                       WHERE {0}.{1}.{3} IN ({4})
                      """.format(self.db, table, select_col, where_col,
                                 ', '.join(['%s'] * len(where_val)))
        else:
            command = """
                      SELECT {0}.{1}.{2}, {0}.{1}.{3}
                       FROM {0}.{1}
                       WHERE {0}.{1}.{3} IN ({4})
                      """.format(self.db, table, select_col, where_col,
                                 ', '.join(['%s'] * len(where_val)))
        self.cursor.execute(command, where_val)
        data = self.cursor.fetchall()
        if select_col == where_col:
            data = pd.DataFrame(data=data, columns=[select_col])
        else:
            data = pd.DataFrame(data=data, columns=[select_col, where_col])
        return data

    def read_rds_table(self, table, where_col, where_val):
        self.connect()
        command = """
                  SELECT *
                   FROM {0}.{1}
                   WHERE {0}.{1}.{2} IN ({3})
                  """.format(self.db, table, where_col,
                             ', '.join(['%s'] * len(where_val)))
        self.cursor.execute(command, where_val)
        data = self.cursor.fetchall()
        self.connect()
        command = """
                  SELECT *
                  FROM information_schema.columns
                  WHERE table_schema = '{0}'
                  AND table_name = '{1}'
                  """.format(self.db, table)
        self.cursor.execute(command)
        columns = self.cursor.fetchall()
        columns = [x[3] for x in columns]
        data = pd.DataFrame(data=data, columns=columns)
        return data

    def update_rows(self, table, set_cols, set_vals, where_col):
        logging.info('Updating ' + str(len(set_vals)) +
                     ' row(s) from ' + table)
        self.connect()
        command = """
                  UPDATE {0}.{1} AS t
                   SET {2}
                   FROM (VALUES {3})
                   AS c({4})
                   WHERE c.{5} = t.{5}
                  """.format(self.db, table,
                             (', '.join(x + ' = c.' + x
                              for x in [where_col] + set_cols)),
                             ', '.join(['%s'] * len(set_vals)),
                             ', '.join([where_col] + set_cols),
                             where_col)
        self.cursor.execute(command, set_vals)
        self.connection.commit()

    def update_rows_two_where(self, table, set_cols, set_vals, where_col,
                              where_col2, where_val2):
        logging.info('Updating ' + str(len(set_vals)) +
                     ' row(s) from ' + table)
        self.connect()
        command = """
                  UPDATE {0}.{1} AS t
                   SET {2}
                   FROM (VALUES {3})
                   AS c({4})
                   WHERE c.{5} = t.{5}
                   AND t.{6} = {7}
                  """.format(self.db, table,
                             (', '.join(x + ' = c.' + x
                              for x in [where_col] + set_cols)),
                             ', '.join(['%s'] * len(set_vals)),
                             ', '.join([where_col] + set_cols),
                             where_col, where_col2, where_val2)
        self.cursor.execute(command, set_vals)
        self.connection.commit()


class DBSchema(object):
    def __init__(self, config_file):
        self.config_file = config_file
        self.full_config_file = config_path + self.config_file
        self.table_list = None
        self.config = None
        self.pk = None
        self.cols = None
        self.fk = None
        self.load_config(self.full_config_file)

    def load_config(self, config_file):
        df = pd.read_csv(config_file)
        self.table_list = df[exc.table].tolist()
        self.config = df.set_index(exc.table).to_dict()
        for col in exc.split_columns:
            self.config[col] = {key: list(str(value).split(',')) for
                                key, value in self.config[col].items()}
        for table in self.table_list:
            for col in exc.dirty_columns:
                clean_dict = self.clean_table_item(table, col,
                                                   exc.dirty_columns[col])
                self.config[col][table] = clean_dict

    def clean_table_item(self, table, config_col, split_char):
        clean_dict = {}
        for item in self.config[config_col][table]:
            if item == str('nan'):
                continue
            cln_item = item.strip().split(split_char)
            if len(cln_item) == 3:
                clean_dict.update({cln_item[0]: [cln_item[1], cln_item[2]]})
            elif len(cln_item) == 2:
                clean_dict.update({cln_item[0]: cln_item[1]})
        return clean_dict

    def set_table(self, table):
        self.pk = self.config[exc.pk][table]
        self.cols = self.config[exc.columns][table]
        self.fk = self.config[exc.fk][table]

    def get_cols_for_export(self, table):
        self.set_table(table)
        fk_list = [self.fk[x][1] for x in self.fk]
        cols_list = self.cols.keys()
        cols_list = [x for x in cols_list if x not in fk_list]
        return fk_list + cols_list


class DFTranslation(object):
    def __init__(self, config_file, data_file):
        self.config_file = config_file
        self.full_config_file = config_path + self.config_file
        self.data_file = data_file
        self.translation = None
        self.db_columns = None
        self.df_columns = None
        self.df = None
        self.sliced_df = None
        self.type_columns = None
        self.translation = None
        self.translation_type = None
        self.text_columns = None
        self.date_columns = None
        self.int_columns = None
        self.real_columns = None
        self.upload_id = None
        self.load_translation(self.full_config_file)
        self.load_df(self.data_file)

    def load_translation(self, config_file):
        df = pd.read_csv(config_file)
        self.db_columns = df[exc.translation_db].tolist()
        self.df_columns = df[exc.translation_df].tolist()
        self.type_columns = df[exc.translation_type].tolist()
        self.translation = dict(zip(df[exc.translation_df],
                                    df[exc.translation_db]))
        self.translation_type = dict(zip(df[exc.translation_db],
                                         df[exc.translation_type]))
        self.text_columns = {k: v for k, v in self.translation_type.items()
                             if v == 'TEXT'}.keys()
        self.date_columns = {k: v for k, v in self.translation_type.items()
                             if v == 'DATE'}.keys()
        self.int_columns = {k: v for k, v in self.translation_type.items()
                            if v == 'INT' or v == 'BIGINT'
                            or v == 'BIGSERIAL'}.keys()
        self.real_columns = {k: v for k, v in self.translation_type.items()
                             if v == 'REAL' or v == 'DECIMAL'}.keys()

    def load_df(self, datafile):
        self.df = pd.read_csv(datafile, encoding='utf-8')
        self.df_columns = [x for x in self.df_columns
                           if x in list(self.df.columns)]
        self.df = self.df[self.df_columns]
        self.df = self.df.rename(columns=self.translation)
        self.get_upload_id()
        self.df = self.clean_types_for_upload(self.df)
        self.add_event_name()
        self.df = self.df.groupby(self.text_columns + self.date_columns +
                                  self.int_columns).sum().reset_index()
        real_columns = [x for x in self.real_columns if x in self.df.columns]
        self.df = self.df[self.df[real_columns].sum(axis=1) != 0].reset_index()
        self.df.replace(['"'], [''], regex=True, inplace=True)

    def get_upload_id(self):
        self.add_upload_cols()
        ul_id_file_path = config_path + exc.upload_id_file
        if not os.path.isfile(ul_id_file_path):
            ul_id_df = pd.DataFrame(columns=[exc.upload_id_col])
            ul_id_df.to_csv(ul_id_file_path, index=False)
        ul_id_df = pd.read_csv(ul_id_file_path)
        if ul_id_df.empty:
            ul_id_df = self.new_upload()
            ul_id_df.to_csv(ul_id_file_path, index=False)
        self.upload_id = ul_id_df[exc.upload_id_col][0].astype(int)
        self.df[exc.upload_id_col] = self.upload_id

    def new_upload(self):
        db = DB(exc.db_config_file)
        upload_df = self.slice_for_upload(exc.upload_cols)
        ul_id_df = db.insert_rds(exc.upload_tbl, exc.upload_cols,
                                 upload_df.values[0], exc.upload_id_col)
        return ul_id_df

    def add_upload_cols(self):
        self.df[exc.upload_last_upload_date] = dt.datetime.today()
        self.df[exc.upload_data_ed] = self.df[exc.event_date].dropna().max()
        self.df[exc.upload_data_sd] = self.df[exc.event_date].dropna().min()
        self.add_upload_name()

    def add_upload_name(self):
        upload_name_items = [x for param in exc.upload_name_param for x in
                             self.df[param].drop_duplicates()]
        upload_name = '_'.join(x for x in upload_name_items)
        self.df[exc.upload_name] = upload_name

    def add_event_name(self):
        self.df[exc.event_name] = (self.df[exc.event_date].astype(str) +
                                   self.df[exc.full_placement_name])
        self.df[exc.plan_name] = self.df[exc.event_name]

    def slice_for_upload(self, columns):
        exp_cols = [x for x in columns if x in list(self.df.columns)]
        sliced_df = self.df[exp_cols].drop_duplicates()
        sliced_df = self.clean_types_for_upload(sliced_df)
        sliced_df = self.remove_zero_rows(sliced_df)
        return sliced_df

    def clean_types_for_upload(self, df):
        for col in df.columns:
            if col not in self.translation_type.keys():
                continue
            data_type = self.translation_type[col]
            df = self.df_col_to_type(df, col, data_type)
        return df

    @staticmethod
    def df_col_to_type(df, col, data_type):
        if data_type == 'TEXT':
            df[col] = df[col].replace(np.nan, 'None')
            df[col] = df[col].astype(unicode)
        if data_type == 'REAL':
            df[col] = df[col].replace(np.nan, 0)
            df[col] = df[col].astype(float)
        if data_type == 'DATE':
            df[col] = pd.to_datetime(df[col], errors='coerce')
            df[col] = df[col].replace(pd.NaT, None)
        if data_type == 'INT':
            df[col] = df[col].replace(np.nan, 0)
            df[col] = df[col].astype(int)
        return df

    def remove_zero_rows(self, df):
        real_cols = [x for x in self.real_columns if x in df.columns]
        if not real_cols:
            return df
        df['realcolsum'] = df[real_cols].sum(axis=1)
        df = df[df['realcolsum'] != 0]
        df = df.drop(['realcolsum'], axis=1)
        return df
