import time

import tables
import numpy as np

from expr import normalize_type, get_missing_value, get_missing_record, hasvalue
from utils import loop_wh_progress, time2str


def get_table_fields(table):
    dtype = table.dtype
    field_types = dtype.fields
    return [(name, normalize_type(field_types[name][0].type))
            for name in dtype.names]

def assertValidFields(s_fields, table, allowed_missing=None):
    # extract types from field description and normalise to python types
    t_fields = get_table_fields(table)

    # check that all required fields are present
    s_names = set(name for name, _ in s_fields)
    t_names = set(name for name, _ in t_fields)
    allowed_missing = set(allowed_missing) if allowed_missing is not None \
                                           else set() 
    missing = s_names - t_names - allowed_missing 
    if missing:
        raise Exception("Missing field(s) in hdf5 input file: %s"
                        % ', '.join(missing))

    # check that types match
    common_t_fields = [(name, type_)
                       for name, type_ in t_fields if name in s_names]
    common_s_fields = [(name, type_)
                       for name, type_ in s_fields if name in t_names] 
    bad_fields = []
    for (name1, t1), (name2, t2) in zip(sorted(common_s_fields),
                                        sorted(common_t_fields)):
        assert name1 == name2
        if t1 != t2:
            bad_fields.append((name1, t2.__name__, t1.__name__))
    if bad_fields:
        bad_fields_str = "\n".join(" - %s: %s instead of %s" % f for f in bad_fields)
        raise Exception("Field types in hdf5 input file differ from those "
                        "defined in the simulation:\n%s" % bad_fields_str)

def add_and_drop_fields(array, output_fields, output_array=None):
    output_dtype = np.dtype(output_fields)
    output_names = set(output_dtype.names)
    input_names = set(array.dtype.names)
    common_fields = output_names & input_names 
    missing_fields = output_names - input_names
    if output_array is None: 
        output_array = np.empty(len(array), dtype=output_dtype)
        for fname in missing_fields:
            output_array[fname] = get_missing_value(output_array[fname])
    else:
        assert output_array.dtype == output_dtype
    for fname in common_fields:
        output_array[fname] = array[fname]
    return output_array

    
def appendTable(input_table, output_table, chunksize=10000, condition=None):
    if input_table.dtype != output_table.dtype:
        output_fields = get_table_fields(output_table)
    else: 
        output_fields = None
    
    rowsleft = len(input_table)
    if not chunksize:
        chunksize = rowsleft

    start = 0
    num_chunks, remainder = divmod(rowsleft, chunksize)
    if remainder > 0:
        num_chunks += 1

    for _ in range(num_chunks):
        stop = start + min(chunksize, rowsleft)
        if condition is not None:
            data = input_table.readWhere(condition, start=start, stop=stop)
        else:
            data = input_table.read(start, stop)
        if output_fields is not None:
            data = add_and_drop_fields(data, output_fields)
        output_table.append(data)
        output_table.flush()
        rowsleft -= chunksize
        start = stop

#    status = {'start': start,
#              'rowsleft': rowsleft}
#
#    def copyChunk(junk1, junk2):
#        start = status['start']
#        stop = start + min(chunksize, status['rowsleft'])
#        if condition is not None:
#            data = input_table.readWhere(condition, start=start, stop=stop)
#        else:
#            data = input_table.read(start, stop)
#        if output_fields is not None:
#            data = add_and_drop_fields(data, output_fields)
#        output_table.append(data)
#        output_table.flush()
#        status['rowsleft'] -= chunksize
#        status['start'] = stop
#    loop_wh_progress(copyChunk, range(num_chunks))

    return output_table

def copyTable(input_table, output_file, output_node, output_fields=None,
              chunksize=10000, condition=None, **kwargs):
    complete_kwargs = {'title': input_table._v_title,
                       'filters': input_table.filters}
    complete_kwargs.update(kwargs)
    if output_fields is None:
        output_dtype = input_table.dtype
    else:
        output_dtype = np.dtype(output_fields)
    output_table = output_file.createTable(output_node, input_table.name, 
                                           output_dtype, **complete_kwargs)
    return appendTable(input_table, output_table, chunksize, condition)


def copyPeriodicTableAndRebuild(input_table, output_file, output_node, 
                                output_fields, input_rows, input_index,
                                max_id_per_period, target_period, 
                                **kwargs):
    complete_kwargs = {'title': input_table._v_title,
                       'filters': input_table.filters}
    complete_kwargs.update(kwargs)
    if output_fields is None:
        output_dtype = input_table.dtype
    else:
        output_dtype = np.dtype(output_fields)
    output_table = output_file.createTable(output_node, input_table.name, 
                                           output_dtype, **complete_kwargs)

    periods_before = [p for p in input_rows.iterkeys() if p <= target_period]
    periods_before.sort()

    print " * computing is present...",
    start_time = time.time()
    max_id = max_id_per_period[periods_before[-1]]
    is_present = np.zeros(max_id + 1, dtype=bool)
    for period in periods_before:
        period_id_to_rownum = input_index[period]
        present_in_period = period_id_to_rownum != -1
        present_in_period.resize(max_id + 1)
        is_present |= present_in_period
    print "done (%s elapsed)." % time2str(time.time() - start_time)
        
    print " * indexing present ids...",
    start_time = time.time()
    id_to_rownum = np.empty(max_id + 1, dtype=int)
    id_to_rownum.fill(-1)

    rownum = 0
    for id, present in enumerate(is_present):
        if present:
            id_to_rownum[id] = rownum
            rownum += 1
    print "done (%s elapsed)." % time2str(time.time() - start_time)

    print " * copying table..."
    start_time = time.time()
    output_array = np.empty(rownum, dtype=output_dtype)
    missing_record = get_missing_record(output_array)
    output_array.fill(missing_record)

    output_rows = {}
    status = {'period_output_array': None}

    def copyPeriod(period_idx, period):
        if period >= target_period:
            return
        start, stop = input_rows[period]
        #TODO: use chunk if there are too many rows in a year
        input_array = input_table.read(start, stop)
        period_output_array = status['period_output_array']
        if period_output_array is not None and \
           len(input_array) == len(period_output_array):
            # reuse period_output_array
            period_output_array = add_and_drop_fields(input_array, 
                                                      output_fields,
                                                      period_output_array)
        else:
            period_output_array = add_and_drop_fields(input_array, 
                                                      output_fields)
        startrow = output_table.nrows
        output_table.append(period_output_array)
        output_rows[period] = (startrow, output_table.nrows)
        output_table.flush()
        status['period_output_array'] = period_output_array

    loop_wh_progress(copyPeriod, periods_before)
        
    print "done (%s elapsed)." % time2str(time.time() - start_time)

    print " * building array for first simulated period...",
    start_time = time.time()
    output_array_source_rows = np.empty(rownum, dtype=int)
    output_array_source_rows.fill(-1)
    for period in periods_before[::-1]:
        start, stop = input_rows[period]
        for id, input_rownum in enumerate(input_index[period]):
            if input_rownum != -1:
                output_rownum = id_to_rownum[id]
                if output_rownum != -1:
                    if output_array_source_rows[output_rownum] == -1:
                        output_array_source_rows[output_rownum] = \
                            start + input_rownum
        if np.all(output_array_source_rows != -1):
            break
    output_array = input_table.readCoordinates(output_array_source_rows)
    output_array = add_and_drop_fields(output_array, output_fields)
    print "done (%s elapsed)." % time2str(time.time() - start_time)

    return output_array, output_rows, id_to_rownum
    

class H5Data(object):
    def __init__(self, input_path, output_path):
        self.input_path = input_path
        self.output_path = output_path
        
    def run(self, entities, start_period):
        last_period = start_period - 1
        print "reading data from %s and storing period %d in memory..." % \
              (self.input_path, last_period)

        input_file = tables.openFile(self.input_path, mode="r")
        output_file = tables.openFile(self.output_path, mode="w")

        input_root = input_file.root

        output_globals = output_file.createGroup("/", "globals", "Globals")
        # load globals in memory
        copyTable(input_root.globals.periodic, output_file, output_globals)
        periodic_globals = input_root.globals.periodic.read()

        input_entities = input_root.entities
        output_entities = output_file.createGroup("/", "entities", "Entities")
    
        for ent_name, entity in entities.iteritems():
            print ent_name, "..."
            
            # main table
            table = getattr(input_entities, ent_name)

            assertValidFields(entity.fields, table, entity.missing_fields)

            # build indexes
            print " * indexing table...",
            start_time = time.time()
            table_index = {}
            current_period = None
            start_row = None
            max_id_per_period = {}
            max_id_so_far = -1
            temp_id_to_rownum = []
            for idx, row in enumerate(table):
                period, id = row['period'], row['id']
                if period != current_period:
                    # 0 > None is True
                    assert period > current_period, "data is not time-ordered"
                    if start_row is not None:
                        table_index[current_period] = start_row, idx
                        # assumes the data is sorted on period then id
                        max_id_per_period[current_period] = max_id_so_far
                        id_to_rownum = np.array(temp_id_to_rownum)
                        entity.input_index[current_period] = id_to_rownum
                        temp_id_to_rownum = [-1] * (max_id_so_far + 1)
                    start_row = idx
                    current_period = period
                if id > max_id_so_far:
                    extra = [-1] * (id - max_id_so_far)
                    temp_id_to_rownum.extend(extra)
                temp_id_to_rownum[id] = idx - start_row
                max_id_so_far = max(max_id_so_far, id)
            table_index[current_period] = (start_row, len(table))
            max_id_per_period[current_period] = max_id_so_far
            entity.input_index[current_period] = np.array(temp_id_to_rownum)

#            periods = sorted(table_index.keys())
            entity.input_rows = table_index
            entity.base_period = min(table_index.keys())
            print "done (%s elapsed)." % time2str(time.time() - start_time)

            # copy stuff
            array, output_rows, id_to_rownum = \
                copyPeriodicTableAndRebuild(table, output_file, output_entities, 
                                            entity.fields, entity.input_rows, 
                                            entity.input_index, 
                                            max_id_per_period, last_period)
            entity.id_to_rownum = id_to_rownum
            entity.output_rows = output_rows
            entity.array = array

            # per period
            per_period_table = getattr(input_entities, ent_name + "_per_period")
            assertValidFields(entity.per_period_fields,
                              per_period_table, entity.pp_missing_fields)
            
            copyTable(per_period_table, output_file, output_entities,
                      entity.per_period_fields)
            per_period_array = per_period_table.readWhere("period==%d" 
                                                          % last_period)
            per_period_array = add_and_drop_fields(per_period_array,
                                                   entity.per_period_fields)
            if not len(per_period_array):
                #TODO: use missing values instead
                per_period_array = np.zeros(1, dtype=per_period_array.dtype)
                per_period_array['period'] = last_period
            
            entity.per_period_array = per_period_array
            
        input_file.close()
        output_file.close()
        return periodic_globals

    
class Void(object):
    def __init__(self, output_path):
        self.output_path = output_path
        
    def run(self, entities, start_period):
        output_file = tables.openFile(self.output_path, mode="w")

        output_globals = output_file.createGroup("/", "globals", "Globals")
        dtype = np.dtype([('period', int)])
        periodic_globals = np.empty(0, dtype=dtype)
        output_file.createTable(output_globals, 'periodic', dtype, title='')

        output_entities = output_file.createGroup("/", "entities", "Entities")

        for ent_name, entity in entities.iteritems():
            dtype = np.dtype(entity.fields)
            entity.array = np.empty(0, dtype=dtype)
            output_file.createTable(output_entities, entity.name, dtype,
                                    title="%s table" % entity.name)

            dtype = np.dtype(entity.per_period_fields)
            entity.per_period_array = np.empty(0, dtype=dtype)
            output_file.createTable(output_entities,
                                    entity.name + "_per_period",
                                    dtype,
                                    title="%s per period table" % entity.name)
        
        output_file.close()
        return periodic_globals

