# -*- coding: utf-8 -*-

from base import Application
from telldus import DeviceManager
from web.base import Server
from lupa import LuaRuntime, lua_type
from threading import Thread, Condition, Lock, Timer
import os, types

# Whitelist functions known to be safe.
safeFunctions = {
	'_VERSION': [],
	'assert': [],
	'coroutine': ['create', 'resume', 'running', 'status', 'wrap', 'yield'],
	'error': [],
	'ipairs': [],
	'math': ['abs', 'acos', 'asin', 'atan', 'atan2', 'ceil', 'cos', 'cosh', 'deg', 'exp', 'floor', 'fmod', 'frexp', 'huge', 'ldexp', 'log', 'log10', 'max', 'min', 'modf', 'pi', 'pow', 'rad', 'random', 'randomseed', 'sin', 'sinh', 'sqrt', 'tan', 'tanh'],
	'next': [],
	'os': ['clock', 'date', 'difftime', 'time'],
	'pairs': [],
	'pcall': [],
	'print': [],
	'python': ['as_attrgetter', 'as_itemgetter', 'as_function', 'enumerate', 'iter', 'iterex'],
	'select': [],
	'string': ['byte', 'char', 'find', 'format', 'gmatch', 'gsub', 'len', 'lower', 'match', 'rep', 'reverse', 'sub', 'upper'],
	'table': ['concat', 'insert', 'maxn', 'remove', 'sort'],
	'tonumber': [],
	'tostring': [],
	'type': [],
	'unpack': [],
	'xpcall': []
}

class List(object):
	"""
	This object is available in Lua code as the object :class:`list` and is
	a helper class for working with Python lists.
	"""
	len = len
	@staticmethod
	def new(*args):
		"""Create a new Python list for use with Python code.

		Example:
		local pythonList = list.new(1, 2, 3, 4)
		"""
		return list(args)

	@staticmethod
	def slice(collection, start = None, end = None, step = None):
		"""Retrieve the start, stop and step indices from the slice object `list`.
		Treats indices greater than length as errors.

		This can be used for slicing python lists (e.g. l[0:10:2])."""
		return collection[slice(start,end,step)]

def sleep(ms):
	"""
	Delay for a specified amount of time.

	  :ms: The number of milliseconds to sleep.

	"""
	pass
	# The real function is not implemented here. This is just for documentation.

class LuaThread(object):
	def __init__(self):
		pass

	def abort(self):
		pass

class SleepingLuaThread(LuaThread):
	def __init__(self, ms):
		super(SleepingLuaThread,self).__init__()
		self.ms = ms
		self.timer = None

	def abort(self):
		if self.timer:
			self.timer.cancel()

	def start(self, cb):
		self.timer = Timer(self.ms/1000.0, cb)
		self.timer.start()

class LuaScript(object):
	CLOSED, LOADING, RUNNING, IDLE, ERROR, CLOSING = range(6)

	def __init__(self, filename, context):
		self.filename = filename
		self.name = os.path.basename(filename)
		self.context = context
		self.runningLuaThread = None
		self.runningLuaThreads = []
		self.__allowedSignals = []
		self.__queue = []
		self.__thread = Thread(target=self.__run, name=self.name)
		self.__threadLock = Condition(Lock())
		self.__state = LuaScript.CLOSED
		self.__stateLock = Lock()
		self.__thread.start()

	def call(self, name, *args):
		if self.state() not in [LuaScript.RUNNING, LuaScript.IDLE]:
			return
		if name not in self.__allowedSignals:
			return
		self.__threadLock.acquire()
		try:
			self.__queue.append((name, args))
			self.__threadLock.notifyAll()
		finally:
			self.__threadLock.release()

	def load(self):
		self.reload()

	def p(self, msg, *args):
		try:
			logMsg = msg % args
		except:
			logMsg = msg
		Server(self.context).webSocketSend('lua', 'log', logMsg)

	def reload(self):
		with open(self.filename, 'r') as f:
			self.code = f.read()
		self.__setState(LuaScript.LOADING)
		self.__notifyThread()

	def shutdown(self):
		self.__setState(LuaScript.CLOSING)
		self.__notifyThread()
		self.__thread.join()
		self.p("Script %s unloaded", self.name)

	def state(self):
		with self.__stateLock:
			return self.__state

	def __notifyThread(self):
		self.__threadLock.acquire()
		try:
			self.__threadLock.notifyAll()
		finally:
			self.__threadLock.release()

	def __run(self):
		while True:
			state = self.state()
			self.__threadLock.acquire()
			task = None
			try:
				if len(self.__queue):
					task = self.__queue.pop(0)
				elif state in [LuaScript.LOADING, LuaScript.CLOSING]:
					# Abort any threads that might be running
					for t in self.runningLuaThreads:
						t.abort()
					self.runningLuaThreads = []
				else:
					self.__threadLock.wait(300)
			finally:
				self.__threadLock.release()
			if state == LuaScript.CLOSING:
				self.__setState(LuaScript.CLOSED)
				return
			if state == LuaScript.LOADING:
				self.__load()
			elif task is not None:
				name, args = task
				if type(name) == str:
					fn = getattr(self.lua.globals(), name)
					self.runningLuaThread = fn.coroutine(*args)
				elif lua_type(name) == 'thread':
					self.runningLuaThread = name
				else:
					continue
				try:
					self.__setState(LuaScript.RUNNING)
					self.runningLuaThread.send(None)
				except StopIteration:
					pass
				except Exception as e:
					self.p("Could not execute function %s: %s", name, e)
				self.runningLuaThread = None
				self.__setState(LuaScript.IDLE)

	def __load(self):
		self.lua = LuaRuntime(
			unpack_returned_tuples=True,
			register_eval=False,
			attribute_handlers=(self.__getter,self.__setter)
		)
		setattr(self.lua.globals(), 'print', self.p)
		# Remove potentially dangerous functions
		self.__sandboxInterpreter()
		# Install a sleep function as lua script since it need to be able to yield
		self.lua.execute('function sleep(ms)\nsuspend(ms)\ncoroutine.yield()\nend')
		setattr(self.lua.globals(), 'suspend', self.__luaSleep)
		self.lua.globals().deviceManager = DeviceManager(self.context)
		try:
			self.__setState(LuaScript.RUNNING)
			self.lua.execute(self.code)
			# Register which signals the script accepts so we don't need to access
			# the interpreter from any other thread. That leads to locks.
			self.__allowedSignals = []
			for func in self.lua.globals():
				if func.startswith('on'):
					self.__allowedSignals.append(func)
			self.__setState(LuaScript.IDLE)
			# Allow script to initialize itself
			self.call('onInit')
			self.p("Script %s loaded", self.name)
		except Exception as e:
			self.__setState(LuaScript.ERROR)
			self.p("Could not execute lua script %s", e)

	def __luaSleep(self, ms):
		co = self.runningLuaThread
		t = SleepingLuaThread(ms)
		def resume():
			if t in self.runningLuaThreads:
				self.runningLuaThreads.remove(t)
			if not bool(co):
				# Cannot call coroutine anymore
				return
			self.__threadLock.acquire()
			try:
				self.__queue.append((co, []))
				self.__threadLock.notifyAll()
			finally:
				self.__threadLock.release()
		t.start(resume)
		self.runningLuaThreads.append(t)

	def __sandboxInterpreter(self):
		for obj in self.lua.globals():
			if obj == '_G':
				# Allow _G here to not start recursion
				continue
			if obj not in safeFunctions:
				del self.lua.globals()[obj]
				continue
			if lua_type(self.lua.globals()[obj]) != 'table':
				continue
			funcs = safeFunctions[obj]
			for func in self.lua.globals()[obj]:
				if func not in funcs:
					del self.lua.globals()[obj][func]
		self.lua.globals().list = List

	def __setState(self, newState):
		with self.__stateLock:
			self.__state = newState

	def __getter(self, obj, attrName):
		# Our getter method is a bit special. Since the lua scripts are executed in
		# it's own thread we cannot call any python code directly. We cannot simply
		# queue it to the main thread either since we must support returning any
		# return value from the functions. The concept here is that attributes are
		# returned directly but any access to functions returns a proxy method
		# instead. A call to the proxy method send a call to the main thread, blocks
		# and then wait for it to be executed.
		if type(attrName) == int:
			# obj is probably a list
			attribute = obj[attrName]
		elif not hasattr(obj, attrName):
			raise AttributeError('object has no attribute "%s"' % attrName)
		else:
			attribute = getattr(obj, attrName)
		if type(attribute) in [int, str, unicode, float, types.NoneType]:
			# Allow primitive attributes directly
			return attribute
		if type(attribute) == types.MethodType:
			# Get the unbound method to support obj:method() calling convention in Lua
			attribute = getattr(obj.__class__, attrName)
		elif type(attribute) not in [types.FunctionType, types.BuiltinFunctionType]:
			raise AttributeError('type "%s" is not allowed in Lua code. Trying to access attribute %s in object %s' % (type(attribute), attrName, obj))
		condition = Condition()
		retval = {}
		def mainThreadCaller(args, kwargs):
			# This is called from the main thread. Do the actual call here
			try:
				retval['return'] = attribute(*args, **kwargs)
			except Exception, e:
				retval['error'] = str(e)
			condition.acquire()
			try:
				condition.notifyAll()
			finally:
				condition.release()

		def proxyMethod(*args, **kwargs):
			# We are in the script thread here, we must syncronize with the main
			# thread before calling the attribute
			condition.acquire()
			try:
				Application().queue(mainThreadCaller, args, kwargs)
				condition.wait(20)  # Timeout to not let the script hang forever
				if 'error' in retval:
					raise AttributeError(retval['error'])
				elif 'return' in retval:
					return retval['return']
			finally:
				condition.release()
			raise AttributeError('The call to the function "%s" timed out' % attrName)
		return proxyMethod

	def __setter(self, obj, attrName, value):
		# Set it in the main thread
		Application().queue(setattr, obj, attrName, value)
