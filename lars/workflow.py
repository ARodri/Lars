#!/usr/bin/env python
import importlib
import sys
import json
import logging
import sys
import os
import time
import argparse
import socket
import re

from feeder import FileReader
from lars import mapper 
from lars.outputter import *
from lars.record import record
from lars import log
#todo- make class vars instead of strings
#TODO: Name the workflows so that you can auto assign record_ids in a multi-threaded environment

ENV_RE = re.compile("<%= ENV\[\'.+?\'\] %>")

def replace_env(wf_str):
    replacements = ENV_RE.findall(wf_str)
    bads = []
    for replace in replacements:
        env_var = replace.split("ENV['")[1].split("']")[0]
        env_val =  os.environ.get(env_var)
        if env_val:
            wf_str = wf_str.replace(replace,env_val)
        else:
            bads.append(env_var)
    if len(bads) != 0:
        raise KeyError("The following environment variables do not exist %s" % bads)
    return wf_str

class Workflow(object):

	
	def __init__(self):
		logger = logging.getLogger('lars.workflow')
		self.timing = False
		self.exceptOnMapperError = True
		self.numProc = 0
		self.context = {
			"hostname":socket.gethostname(),
			"record_id":"STARTUP",
			"record":None,
			"applicationname":"lars"
			}
		self.logger = log.LarsLoggerAdapter(logger,self.context)
                    

	def buildJSON(self, config,instanceID=None):
		self.context['workflow'] = instanceID
		wf= config['workflow']
		self.getRecordIDField = wf.get("get_recordID_field",None)
		self.putRecordIDField = wf.get("put_recordID_field",None)
		self.mappers = []
		self.mapperDict = {}
		try:
			for mapperConfig in wf['mappers']:
				thisMap,skip = mapper.JSONMapperBuilder.buildFromJSON(mapperConfig, self.context)
				if skip:
					self.logger.info("skipping %s" % thisMap.name)
				else:
					mapperTup = (thisMap,[])
					self.mappers.append(mapperTup)
					self.mapperDict[thisMap.name] = mapperTup
					self.logger.info("Parsed %s" % thisMap.name)
			for outConfig in wf.get('outputters',[]):
				#default outputter is delimited outputter
				thisOutClass = DelimitedOutputter
				if outConfig.has_key("class"):
					module_name,class_name = outConfig["class"].split(".")
					thisMod= importlib.import_module(module_name)
					thisOutClass = getattr(thisMod,class_name)
				thisOut = thisOutClass()
				thisOut.loadConfigJSON(outConfig,instanceID=instanceID)
				if thisOut.after==None:
					self.mappers[-1][1].append(thisOut)
				else:
					self.mapperDict[thisOut.after][1].append(thisOut)
		except mapper.MapperConfigurationException, e:
			self.logger.error(e)
			sys.exit(1)

		self.modify_response = False
		self.response_fields = []
		if 'response_format' in wf: 
			self.modify_response = True
			with open(wf['response_format'],'r') as fmt:
				for line in fmt:
					self.response_fields.append(line.strip())

	def enableTiming(self):
		self.timing = True
	def disableTiming(self):
		self.timing = False
	def enableExceptionHandling(self):
		self.exceptOnMapperError = False
	def disableExceptionHandling(self):
		self.exceptOnMapperError = True

	def stop(self):
		for (mapper,outputts) in self.mappers:
			mapper.stop()

	def process(self,recordDict):
		thisRec = record(recordDict)
		#if a record_id is provided in the original dict use that, else use the 
		#number of records that have been processed thus far
		if self.getRecordIDField:
			try:
				thisRec.set_record_id(thisRec[self.getRecordIDField])
			except KeyError:
				raise KeyError("recordID cannot be assigned, %s not present in input" % self.getRecordIDField) 
		else:
			thisRec.set_record_id(self.numProc)
		#assign the record_id to a field in the underlying dict if requested
		if self.putRecordIDField:
			thisRec[self.putRecordIDField] = thisRec.get_record_id()
		#change the context for logging 
		self.context["record_id"] = thisRec.get_record_id()
		thisRec.set_context(self.context)
		start = time.time()
		if self.mappers == None:
			raise Exception("not built")
		
		start = time.time()
		i=0
		#run through all the mappers and outputters
		for mapper,outputters in self.mappers:
			self.logger.debug("Sending to %s" % mapper.name)
			thisRec = mapper.processWrapper(thisRec,True,self.exceptOnMapperError)
			self.logger.debug("Done with %s" % mapper.name)

			if i == len(self.mappers) - 1:
				end = time.time()
				thisRec['TOTAL_TIME'] = ((end-start)*1000)
			for outputter in outputters:
				outputter.output(thisRec)
			i+=1
		#increment the processed records
		self.numProc+=1
		dur = (time.time()-start)*1000
		self.logger.info("Processed in %0.2fms" % dur)
		return self.make_response(thisRec)

	def make_response(self, thisRec):
		if not self.modify_response:
			return thisRec
		else:
			resp = record({})
			resp.set_context(thisRec.get_context())
			for field in self.response_fields:
				if field in thisRec:
					resp[field] = thisRec[field]
				else:
					raise KeyError('Missing output field %s from record' % field)
			return resp


if __name__ == "__main__":

	parser = argparse.ArgumentParser(description='Simple single-threaded interface for running a workflow')

	parser.add_argument('input_file', nargs=1, type=argparse.FileType('r'), help='Input file. Either delimited with a header OR 1-JSON-per-line')
	parser.add_argument('workflow', nargs=1, type=argparse.FileType('r'), help='Workflow JSON configuration.')
	parser.add_argument('--loglevel', metavar='LEVEL', choices=["INFO","DEBUG","WARNING","ERROR"], default='INFO', help='Logging level: %(choices)s (default: %(default)s)')
	parser.add_argument('-d', default='|', metavar='DELIM', help='Input file delimiter. If the file is already JSON format (one line per record) specify "JSON". (default: %(default)s)')

	args = parser.parse_args()

	log.configure_json_stderr(level=args.loglevel)

	wf = Workflow()
	with args.workflow[0] as wfFH:
                workflow_str = replace_env(wfFH.read())
		wf.buildJSON(json.loads(workflow_str))

	with args.input_file[0] as recordFH:
		reader = FileReader(recordFH, args.d)
		for r in reader:
			wf.logger.debug(wf.process(r))


