#!/usr/bin/env python


# Hi, and welcome to the code.
# This is currently PoC quality, and I'm no Python God either.
# Patches are encouraged, fork me on GitHub.


import xml.dom.minidom
from xml.parsers.expat import ParserCreate
from calendar import timegm
import codecs
import datetime
import os
import socket
import struct
import sys
import time
import urlparse
from optparse import OptionParser

# How many bytes to read at once. You probably can leave this alone.
# FIXME: With smaller READ_SIZE this tends to crash on the final read?
READ_SIZE = 10240000
# The encoding for input, output and internal representation. Leave alone.
ENCODING = 'UTF-8'


def parse_args(args):
	usage = 'Usage: git init --bare repo && bzcat pages-meta-history.xml.bz2 | \\\n' \
	        '       %prog [options] | GIT_DIR=repo git fast-import | sed \'s/^progress //\''
	parser = OptionParser(usage=usage)
	parser.add_option("-m", "--max", dest="IMPORT_MAX", metavar="IMPORT_MAX",
			help="Specify the maxium pages to import, -1 for all (default: 100)",
			default=100, type="int")
	parser.add_option("-d", "--deepness", dest="DEEPNESS", metavar="DEEPNESS",
			help="Specify the deepness of the result directory structure (default: 3)",
			default=3, type="int")
	parser.add_option("-c", "--committer", dest="COMMITTER", metavar="COMITTER",
			help="git \"Committer\" used while doing the commits (default: \"Levitation <levitation@scytale.name>\")",
			default="Levitation <levitation@scytale.name>")
	parser.add_option("-M", "--metafile", dest="METAFILE", metavar="META",
			help="File for storing meta information (17 bytes/rev) (default: .import-meta)",
			default=".import-meta")
	parser.add_option("-C", "--commfile", dest="COMMFILE", metavar="COMM",
			help="File for storing comment information (257 bytes/rev) (default: .import-comm)",
			default=".import-comm")
	parser.add_option("-U", "--userfile", dest="USERFILE", metavar="USER",
			help="File for storing author information (257 bytes/author) (default: .import-user)",
			default=".import-user")
	parser.add_option("-P", "--pagefile", dest="PAGEFILE", metavar="PAGE",
			help="File for storing page information (257 bytes/page) (default: .import-page)",
			default=".import-page")
	(options, args) = parser.parse_args(args)
	return (options, args)

def tzoffset():
	r = time.strftime('%z')
	if r == '' or r == '%z':
		return None
	return r

def tzoffsetorzero():
	r = tzoffset()
	if r == None:
		return '+0000'
	return r

def singletext(node):
	if len(node.childNodes) == 0:
		return ''
	if len(node.childNodes) != 1:
		raise Exception('singletext has wrong number of children' + node.toxml())
	if node.childNodes[0].nodeType != node.TEXT_NODE:
		raise Exception('singletext child is not text')
	return node.childNodes[0].data

def asciiize_char(s):
	r = ''
	for x in s.group(0):
		r += '.' + x.encode('hex').upper()
	return r

def asciiize(s):
	return re.sub('[^A-Za-z0-9_ ()-]', asciiize_char, s)

def out(text):
	sys.stdout.write(text)

def progress(text):
	out('progress ' + text + '\n')
	sys.stdout.flush()

def log(text):
	sys.stderr.write(text)
	sys.stderr.write("\n")
	sys.stderr.flush()


class Meta:
	def __init__(self, file):
		self.struct = struct.Struct('LLLLB')
		self.maxrev = -1
		self.fh = open(file, 'wb+')
		self.domain = 'unknown.invalid'
		self.nstoid = self.idtons = {}
	def write(self, rev, time, page, author, minor):
		flags = 0
		if minor:
			flags += 1
		if author.isip:
			flags += 2
		if author.isdel:
			flags += 4
		data = self.struct.pack(
			rev,
			timegm(time.utctimetuple()),
			page,
			author.id,
			flags
			)
		self.fh.seek(rev * self.struct.size)
		self.fh.write(data)
		if self.maxrev < rev:
			self.maxrev = rev
	def read(self, rev):
		self.fh.seek(rev * self.struct.size)
		data = self.fh.read(self.struct.size)
		tuple = self.struct.unpack(data)
		d = {
			'rev':    tuple[0],
			'epoch':  tuple[1],
			'time':   datetime.datetime.utcfromtimestamp(tuple[1]),
			'page':   tuple[2],
			'user':   tuple[3],
			'minor':  False,
			'isip':   False,
			'isdel':  False,
			}
		if d['rev'] != 0:
			d['exists'] = True
		else:
			d['exists'] = False
		d['day'] = d['time'].strftime('%Y-%m-%d')
		flags = tuple[4]
		if flags & 1:
			d['minor'] = True
		if flags & 2:
			d['isip'] = True
			d['user'] = socket.inet_ntoa(struct.pack('!I', tuple[3]))
		if flags & 4:
			d['isdel'] = True
		return d

class StringStore:
	max_length = 255
	def __init__(self, file):
		self.struct = struct.Struct('Bb255s')
		self.maxid = -1
		self.fh = open(file, 'wb+')
	def write(self, id, text, flags = 1):
		if len(text) > 255:
			progress('warning: trimming %s bytes long text: 0x%s' % (len(text), text.encode('hex')))
			text = text[0:255].decode(ENCODING, 'ignore').encode(ENCODING)
		data = self.struct.pack(len(text), flags, text)
		self.fh.seek(id * self.struct.size)
		self.fh.write(data)
		if self.maxid < id:
			self.maxid = id
	def read(self, id):
		self.fh.seek(id * self.struct.size)
		packed = self.fh.read(self.struct.size)
		data = None
		if len(packed) < self.struct.size:
			# There is no such entry.
			d = {'len': 0, 'flags': 0, 'text': ''}
		else:
			data = self.struct.unpack(packed)
			d = {
				'len':   data[0],
				'flags': data[1],
				'text':  data[2][0:data[0]]
				}
		return d

class User:
	def __init__(self, node, meta):
		self.id = -1
		self.name = None
		self.isip = self.isdel = False
		if node.hasAttribute('deleted') and node.getAttribute('deleted') == 'deleted':
			self.isdel = True
		for lv1 in node.childNodes:
			if lv1.nodeType != lv1.ELEMENT_NODE:
				continue
			if lv1.tagName == 'username':
				self.name = singletext(lv1).encode(ENCODING)
			elif lv1.tagName == 'id':
				self.id = int(singletext(lv1))
			elif lv1.tagName == 'ip':
				# FIXME: This is so not-v6-compatible it hurts.
				self.isip = True
				try:
					self.id = struct.unpack('!I', socket.inet_aton(singletext(lv1)))[0]
				except (socket.error, UnicodeEncodeError):
					# IP could not be parsed. Leave ID as -1 then.
					pass
		if not (self.isip or self.isdel):
			meta['user'].write(self.id, self.name)

class Revision:
	def __init__(self, node, page, meta):
		self.id = -1
		self.minor = False
		self.timestamp = self.text = self.comment = self.user = None
		self.page = page
		self.meta = meta
		self.dom = node
		for lv1 in self.dom.childNodes:
			if lv1.nodeType != lv1.ELEMENT_NODE:
				continue
			if lv1.tagName == 'id':
				self.id = int(singletext(lv1))
			elif lv1.tagName == 'timestamp':
				self.timestamp = datetime.datetime.strptime(singletext(lv1), "%Y-%m-%dT%H:%M:%SZ")
			elif lv1.tagName == 'contributor':
				self.user = User(lv1, self.meta)
			elif lv1.tagName == 'minor':
				self.minor = True
			elif lv1.tagName == 'comment':
				self.comment = singletext(lv1)
			elif lv1.tagName == 'text':
				self.text = singletext(lv1)
	def dump(self):
		self.meta['meta'].write(self.id, self.timestamp, self.page, self.user, self.minor)
		if self.comment:
			self.meta['comm'].write(self.id, self.comment.encode(ENCODING))
		mydata = self.text.encode(ENCODING)
		out('blob\nmark :%d\ndata %d\n' % (self.id + 1, len(mydata)))
		out(mydata + '\n')

class Page:
	def __init__(self, dom, meta):
		self.revisions = []
		self.id = -1
		self.nsid = 0
		self.title = self.fulltitle = ''
		self.meta = meta
		self.dom = dom
		for lv1 in self.dom.documentElement.childNodes:
			if lv1.nodeType != lv1.ELEMENT_NODE:
				continue
			if lv1.tagName == 'title':
				self.fulltitle = singletext(lv1).encode(ENCODING)
				split = self.fulltitle.split(':', 1)
				if len(split) > 1 and self.meta['meta'].nstoid.has_key(split[0]):
					self.nsid = self.meta['meta'].nstoid[split[0]]
					self.title = split[1]
				else:
					self.nsid = self.meta['meta'].nstoid['']
					self.title = self.fulltitle
			elif lv1.tagName == 'id':
				self.id = int(singletext(lv1))
			elif lv1.tagName == 'revision':
				self.revisions.append(Revision(lv1, self.id, self.meta))
		self.meta['page'].write(self.id, self.title, self.nsid)
	def dump(self):
		progress('   ' + self.fulltitle)
		for revision in self.revisions:
			revision.dump()

class BlobWriter:
	def __init__(self, meta):
		self.text = self.xml = None
		self.intag = self.lastattrs = None
		self.cancel = False
		self.startbyte = self.readbytes = self.imported = 0
		self.meta = meta
		self.fh = codecs.getreader(ENCODING)(sys.stdin)
		self.expat = ParserCreate(ENCODING)
		self.expat.StartElementHandler = self.find_start
	def parse(self):
		while True:
			self.text = self.fh.read(READ_SIZE).encode(ENCODING)
			if not self.text:
				break
			self.startbyte = 0
			self.expat.Parse(self.text)
			if self.cancel:
				return
			if self.intag:
				self.xml += self.text[self.startbyte:]
			self.readbytes += len(self.text)
		self.expat.Parse('', True)
	def find_start(self, name, attrs):
		if name in ('page', 'base', 'namespace'):
			self.intag = name
			self.lastattrs = attrs
			self.expat.StartElementHandler = None
			self.expat.EndElementHandler = self.find_end
			self.startbyte = self.expat.CurrentByteIndex - self.readbytes
			self.xml = ''
	def find_end(self, name):
		if name == self.intag:
			self.intag = None
			self.expat.StartElementHandler = self.find_start
			self.expat.EndElementHandler = None
			endbyte = self.expat.CurrentByteIndex - self.readbytes
			self.xml += self.text[self.startbyte:endbyte] + '</' + name.encode(ENCODING) + '>'
			if self.startbyte == endbyte:
				self.xml = '<' + name.encode(ENCODING) + ' />'
			dom = xml.dom.minidom.parseString(self.xml)
			if name == 'page':
				Page(dom, meta).dump()
				self.imported += 1
				max = self.meta['options'].IMPORT_MAX
				if max > 0 and self.imported >= max:
					self.expat.StartElementHandler = None
					self.cancel = True
			elif name == 'base':
				self.meta['meta'].domain = urlparse.urlparse(singletext(dom.documentElement)).hostname.encode(ENCODING)
			elif name == 'namespace':
				k = int(self.lastattrs['key'])
				v = singletext(dom.documentElement).encode(ENCODING)
				self.meta['meta'].idtons[k] = v
				self.meta['meta'].nstoid[v] = k

class Committer:
	def __init__(self, meta):
		self.meta = meta
		if tzoffset() == None:
			progress('warning: using %s as local time offset since your system refuses to tell me the right one;' \
				'commit (but not author) times will most likely be wrong' % tzoffsetorzero())
	def work(self):
		rev = commit = 1
		day = ''
		while rev <= self.meta['meta'].maxrev:
			meta = self.meta['meta'].read(rev)
			rev += 1
			if not meta['exists']:
				continue
			page = self.meta['page'].read(meta['page'])
			comm = self.meta['comm'].read(meta['rev'])
			namespace = asciiize('%d-%s' % (page['flags'], self.meta['meta'].idtons[page['flags']]))
			title = page['text']
			subdirtitle = ''
			for i in range(0, min(self.meta['options'].DEEPNESS, len(title))):
				subdirtitle += asciiize(title[i]) + '/'
			subdirtitle += asciiize(title)
			filename = namespace + '/' + subdirtitle + '.mediawiki'
			if meta['minor']:
				minor = ' (minor)'
			else:
				minor = ''
			msg = comm['text'] + '\n\nLevitation import of page %d rev %d%s.\n' % (meta['page'], meta['rev'], minor)
			if commit == 1:
				fromline = ''
			else:
				fromline = 'from :%d\n' % (commit - 1)
			if day != meta['day']:
				day = meta['day']
				progress('   ' + day)
			if meta['isip']:
				author = meta['user']
				authoruid = 'ip-' + author
			elif meta['isdel']:
				author = '[deleted user]'
				authoruid = 'deleted'
			else:
				authoruid = 'uid-' + str(meta['user'])
				author = self.meta['user'].read(meta['user'])['text']
			out(
				'commit refs/heads/master\n' +
				'mark :%d\n' % commit +
				'author %s <%s@git.%s> %d +0000\n' % (author, authoruid, self.meta['meta'].domain, meta['epoch']) +
				'committer %s %d %s\n' % (self.meta['options'].COMMITTER, time.time(), tzoffsetorzero()) +
				'data %d\n%s\n' % (len(msg), msg) +
				fromline +
				'M 100644 :%d %s\n' % (meta['rev'] + 1, filename)
				)
			commit += 1


(options, _args) = parse_args(sys.argv[1:])

meta = { # FIXME: Use parameters.
	'options': options,
	'meta': Meta(options.METAFILE),
	'comm': StringStore(options.COMMFILE),
	'user': StringStore(options.USERFILE),
	'page': StringStore(options.PAGEFILE),
	}

progress('Step 1: Creating blobs.')
BlobWriter(meta).parse()

progress('Step 2: Writing commits.')
Committer(meta).work()
