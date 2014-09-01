#
#    Ucollect - small utility for real-time analysis of network data
#    Copyright (C) 2014 CZ.NIC, z.s.p.o. (http://www.nic.cz/)
#
#    This program is free software; you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation; either version 2 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License along
#    with this program; if not, write to the Free Software Foundation, Inc.,
#    51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#

from twisted.internet import reactor
from twisted.internet.task import LoopingCall
import plugin
import struct
import logging
import activity
import database
import socket
import re

logger = logging.getLogger(name='flow')
token_re = re.compile('\(?\s*(.*?)\s*([,\(\)])(.*)')

filter_index = {}

class Filter:
	def parse(self, code, param):
		self._code = code
		return param

	def serialize(self):
		return self._code

	def token(self, param):
		match = token_re.match(param)
		if match:
			result = (match.group(1), match.group(2), match.group(3))
		else:
			result = (None, None, None)
		logger.trace("Token@%s: %s: %s", self._code, param, repr(result))
		return result

	def get_subs(self, param):
		self._subs = []
		(tok, sep, rest) = self.token(param)
		while tok is not None:
			if tok != '':
				sub = filter_index[tok]()
				param = sub.parse(tok, rest)
				self._subs.append(sub)
			else:
				param = rest
			if sep != ')':
				(tok, sep, rest) = self.token(param)
			else:
				tok = None
		return param

	def get_values(self, param):
		values = []
		(tok, sep, rest) = self.token(param)
		while tok is not None:
			if tok != '':
				values.append(tok)
			param = rest
			if sep != ')':
				(tok, sep, rest) = self.token(param)
			else:
				tok = None
		return values, param

	def __str__(self):
		return self._code

class FilterSubs(Filter):
	def serialize(self):
		return self._code + struct.pack('!I', len(self._subs)) + ''.join(map(lambda f: f.serialize(), self._subs))

	def parse(self, code, param):
		self._code = code
		return self.get_subs(param)

	def __str__(self):
		return self._code + '(' + ','.join(map(str, self._subs)) + ')'

class Filter1Sub(FilterSubs):
	def serialize(self):
		return self._code + self._subs[0].serialize()

class FilterPort(Filter):
	def serialize(self):
		return self._code + struct.pack('!' + str(len(self._ports) + 1) + 'H', len(self._ports), *self._ports)

	def parse(self, code, param):
		self._code = code
		(ports, param) = self.get_values(param)
		self._ports = map(int, ports)
		return param

	def __str__(self):
		return self._code + '(' + ','.join(map(str, self._ports)) + ')'

def encode_ip(ip):
	try:
		return struct.pack('!B', 4) + socket.inet_pton(socket.AF_INET, ip)
	except Exception:
		return struct.pack('!B', 16) + socket.inet_pton(socket.AF_INET6, ip)

class FilterIP(Filter):
	def serialize(self):
		return self._code + struct.pack('!I', len(self._ips)) + ''.join(map(lambda ip: encode_ip(ip), self._ips))

	def parse(self, code, param):
		self._code = code
		(self._ips, param) = self.get_values(param)
		return param

	def __str__(self):
		return self._code + '(' + ','.join(self._ips) + ')'

filter_index = {
	'T': Filter,
	'F': Filter,
	'!': Filter1Sub,
	'&': FilterSubs,
	'|': FilterSubs,
	'i': FilterIP,
	'I': FilterIP,
	'p': FilterPort,
	'P': FilterPort
}

def store_flows(client, message, expect_conf_id):
	(header, message) = (message[:12], message[12:])
	(conf_id, calib_time) = struct.unpack('!IQ', header)
	if conf_id != expect_conf_id:
		logger.warn('Flows of different config (%s vs. %s) received from client %s', conf_id, expect_conf_id, client)
	if not message:
		logger.warn('Empty list of flows from %s', client)
		return
	values = []
	while message:
		(flow, message) = (message[:61], message[61:])
		(flags, cin, cout, sin, sout, ploc, prem, tbin, tbout, tein, teout) = struct.unpack('!BIIQQHHQQQQ', flow)
		v6 = flags & 1
		udp = flags & 2
		if v6:
			size = 16
			tp = socket.AF_INET6
		else:
			size = 4
			tp = socket.AF_INET
		(aloc, arem, message) = (message[:size], message[size:2 * size], message[2 * size:])
		(aloc, arem) = map (lambda addr: socket.inet_ntop(tp, addr), (aloc, arem))
		if udp:
			proto = 'U'
		else:
			proto = 'T'
		logger.trace("Flow times: %s, %s, %s, %s, %s (%s/%s packets)", calib_time, tbin, tbout, tein, teout, cin, cout)
		if cin:
			ok = True
			for v in (tbin, tein):
				if calib_time - v > 86400000:
					logger.error("Time difference out of range for client %s and in direction: %s", client, calib_time - v)
				ok = False
			if ok:
				values.append((arem, aloc, prem, ploc, proto, calib_time - tbin, calib_time - tein, calib_time - tbout if cout else None, sin, cin, True, client))
		if cout:
			ok = True
			for v in (tbout, teout):
				if calib_time - v > 86400000:
					logger.error("Time difference out of range for client %s and out direction: %s", client, calib_time - v)
					ok = False
			if ok:
				values.append((aloc, arem, ploc, prem, proto, calib_time - tbout, calib_time - teout, calib_time - tbin if cin else None, sout, cout, False, client))
	with database.transaction() as t:
		t.executemany("INSERT INTO flows (client, ip_from, ip_to, port_from, port_to, proto, start, stop, opposite_start, size, count, inbound) SELECT clients.id, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP AT TIME ZONE 'UTC' - %s * INTERVAL '1 millisecond', CURRENT_TIMESTAMP AT TIME ZONE 'UTC' - %s * INTERVAL '1 millisecond', CURRENT_TIMESTAMP AT TIME ZONE 'UTC' - %s * INTERVAL '1 millisecond', %s, %s, %s FROM clients WHERE clients.name = %s", values)

class FlowPlugin(plugin.Plugin):
	"""
	Plugin for storing netflow information.
	"""
	def __init__(self, plugins, config):
		plugin.Plugin.__init__(self, plugins)
		self.__config = {}
		self.__conf_checker = LoopingCall(self.__check_conf)
		self.__conf_checker.start(60, True)
		self.__filters = {}
		self.__filter_cache = {}

	def __check_conf(self):
		with database.transaction() as t:
			t.execute("SELECT name, value FROM config WHERE plugin = 'flow'")
			config = dict(t.fetchall())
			# Doh. We just want the newest version in the newest epoch of each filter name.
			# There just have to be a simpler way to say this!
			t.execute('''SELECT
				filters.filter, MAX(filters.epoch), MAX(flow_filters.version)
			FROM
				flow_filters
			JOIN
				(SELECT filter, MAX(epoch) AS epoch FROM flow_filters GROUP BY filter) AS filters
			ON flow_filters.filter = filters.filter AND filters.epoch = flow_filters.epoch
			GROUP BY filters.filter''')
			filters = {}
			for f in t.fetchall():
				(name, epoch, version) = f
				filters[name] = (epoch, version)
		if self.__config != config:
			logger.info('Config changed, broadcasting')
			self.__config = config
			self.broadcast(self.__build_config(''), lambda version: version < 2)
			self.broadcast(self.__build_config('-diff'), lambda version: version >= 2)
			for f in filters:
				self.broadcast(self.__build_filter_version(f, filters[f][0], filters[f][1]), lambda version: version >= 2)
			self.__filter_cache = {}
		else:
			for f in filters:
				# It should not happen there are different filters than in the previous version,
				# such thing should happen only with config change. But we still do it by the new ones
				# so we send the appearing ones. Not mentioning disapearing ones is OK and actually
				# what we want.
				if f not in self.__filters or filters[f] != self.__filters[f]:
					self.broadcast(self.__build_filter_version(f, filters[f][0], filters[f][1]), lambda version: version >= 2)
					self.__filter_cache = {}
		self.__filters = filters

	def __build_filter_version(self, name, epoch, version):
		return 'U' + struct.pack('!I' + str(len(name)) + 'sII', len(name), name, epoch, version)

	def __build_config(self, filter_suffix):
		filter_data = ''
		fil = self.__config['filter' + filter_suffix]
		if fil:
			f = filter_index[fil[0]]()
			f.parse(fil[0], fil[1:])
			logger.debug('Filter: %s', f)
			filter_data = f.serialize()
		return 'C' + struct.pack('!IIII', int(self.__config['version']), int(self.__config['max_flows']), int(self.__config['timeout']), int(self.__config['minpackets'])) + filter_data

	def __build_filter_diff(self, name, full, epoch, from_version, to_version):
		key = (name, full, epoch, from_version, to_version)
		if key in self.__filter_cache:
			return self.__filter_cache[key]
		with database.transaction() as t:
			t.execute('''SELECT
				flow_filters.address, add
			FROM
				(SELECT
					address, MAX(version) AS version
				FROM
					flow_filters
				WHERE
					filter = %s AND epoch = %s AND version > %s AND version <= %s
				GROUP BY
					address)
				AS lasts
			JOIN
				flow_filters
			ON
				flow_filters.address = lasts.address AND flow_filters.version = lasts.version
			WHERE
				filter = %s AND epoch = %s''', (name, epoch, from_version, to_version, name, epoch))
			addresses = t.fetchall()
		params = [len(name), name, full, epoch]
		if not full:
			params.append(from_version)
		params.append(to_version)
		data = 'D' + struct.pack('I' + str(len(name)) + '?II' + ('' if full else 'I'), params)
		for addr in addresses:
			(address, add) = addr
			add = 1 if add else 0
			# Try parsing the string. First as IPv4, then IPv6, IPv4:port, IPv6:port
			try:
				data += struct.pack('!B', 4 + add) + socket.inet_pton(socket.AF_INET, address)
			except Exception:
				try:
					data += struct.pack('!B', 16 + add) + socket.inet_pton(socket.AF_INET6, address)
				except Exception:
					(ip, port) = address.rsplit(':', 1)
					ip = ip.strip('[]')
					try:
						data += struct.pack('!B', 6 + add) + socket.inet_pton(socket.AF_INET, address) + struct.pack('!H', int(port))
					except Exception:
						data += struct.pack('!B', 18 + add) + socket.inet_pton(socket.AF_INET6, address) + struct.pack('!H', int(port))
		self.__filter_cache[key] = data
		return data

	def message_from_client(self, message, client):
		if message[0] == 'C':
			logger.debug('Sending config to %s', client)
			self.send(self.__build_config(), client)
		elif message[0] == 'D':
			logger.debug('Flows from %s', client)
			activity.log_activity(client, 'flow')
			reactor.callInThread(store_flows, client, message[1:], int(self.__config['version']))
		elif message[0] == 'U':
			# Client is requesting difference in a filter
			(full, name_len) = struct.unpack('!?I', message[1:6])
			name = message[6:6+name_len]
			message = message[6+name_len:]
			l = 2 if full else 3
			numbers = struct.unpack(str(l) + 'I')
			if full:
				(epoch, to_version) = numbers
				from_version = 0
			else:
				(epoch, from_version, to_version) = numbers
			logger.debug('Sending diff for filter %s@%s from %s to %s to client %s', name, epoch, from_version, to_version, client)
			self.send(self.__build_filter_diff(name, full, epoch, from_version, to_version), client)

	def name(self):
		return 'Flow'
