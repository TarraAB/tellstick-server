﻿# -*- coding: utf-8 -*-

from base import Application, implements, Plugin, Settings, slot, ISignalObserver
from calendar import timegm
from datetime import date, datetime, timedelta
from events.base import IEventFactory, Action, Condition, Trigger
from pytz import timezone
from SunCalculator import SunCalculator
from telldus import Device, DeviceManager
import pytz
import threading
import time

class TimeTriggerManager(object):
	def __init__(self):
		self.running = False
		self.timeLock = threading.Lock()
		self.triggers = {}
		Application().registerShutdown(self.stop)
		self.thread = threading.Thread(target=self.run)
		self.thread.start()
		self.s = Settings('telldus.scheduler')
		self.timezone = self.s.get('tz', 'UTC')

	def addTrigger(self, trigger):
		with self.timeLock:
			if not trigger.minute in self.triggers:
				self.triggers[trigger.minute] = []
			self.triggers[trigger.minute].append(trigger)

	def clearAll(self):
		with self.timeLock:
			self.triggers = {}  # empty all running triggers

	def deleteTrigger(self, trigger):
		with self.timeLock:
			for minute in self.triggers:
				try:
					self.triggers[minute].remove(trigger)
				except:
					pass

	def recalcAll(self):
		# needs to recalc all triggers, for example longitude/latitude/timezone has changed
		triggersToRemove = {}
		for minute in self.triggers:
			for trigger in self.triggers[minute]:
				if trigger.recalculate():
					# trigger was updated (new minute), move it around
					if minute not in triggersToRemove:
						triggersToRemove[minute] = []
					triggersToRemove[minute].append(trigger)

		with self.timeLock:
			for minute in triggersToRemove:
				for trigger in triggersToRemove[minute]:
					self.triggers[minute].remove(trigger)
					if trigger.minute not in self.triggers:
						self.triggers[trigger.minute] = []
					self.triggers[trigger.minute].append(trigger)

	def run(self):
		self.running = True
		self.lastMinute = None
		while self.running:
			local_timezone = timezone(self.timezone)
			local_time =  datetime.now(local_timezone)
			currentMinute = local_time.minute
			if self.lastMinute is None or self.lastMinute is not currentMinute:
				# new minute, check triggers
				self.lastMinute = currentMinute
				if currentMinute not in self.triggers:
					continue
				triggersToRemove = []
				for trigger in self.triggers[currentMinute]:
					if trigger.hour == -1 or trigger.hour == local_time.hour:
						triggertype = 'time'
						if type(trigger) is SuntimeTrigger:
							triggertype = 'suntime'
						elif type(trigger) is BlockheaterTrigger:
							triggertype = 'blockheater'

						if type(trigger) is SuntimeTrigger and trigger.recalculate():
							# suntime (time or active-status) was updated (new minute), move it around
							triggersToRemove.append(trigger)
						if trigger.active:
							# is active (not inactive due to sunrise/sunset-thing)
							trigger.triggered({'triggertype': triggertype})
				with self.timeLock:
					for trigger in triggersToRemove:
						self.triggers[currentMinute].remove(trigger)
						if not trigger.active:
							continue
						if trigger.minute not in self.triggers:
							self.triggers[trigger.minute] = []
						self.triggers[trigger.minute].append(trigger)
			time.sleep(5)

	def stop(self):
		self.running = False

class TimeTrigger(Trigger):
	def __init__(self, manager, **kwargs):
		super(TimeTrigger,self).__init__(**kwargs)
		self.manager = manager
		self.minute = None
		self.hour = None
		self.setHour = None  # this is the hour actually set (not recalculated to UTC)
		self.active = True  # TimeTriggers are always active
		self.s = Settings('telldus.scheduler')
		self.timezone = self.s.get('tz', 'UTC')

	def close(self):
		self.manager.deleteTrigger(self)

	def parseParam(self, name, value):
		if name == 'minute':
			self.minute = int(value)
		elif name == 'hour':
			self.setHour = int(value)
			# recalculate hour to UTC
			if int(value) == -1:
				self.hour = int(value)
			else:
				local_timezone = timezone(self.timezone)
				currentDate = pytz.utc.localize(datetime.utcnow())
				local_datetime = local_timezone.localize(datetime(currentDate.year, currentDate.month, currentDate.day, int(value)))
				utc_datetime = pytz.utc.normalize(local_datetime.astimezone(pytz.utc))
				if datetime.now().hour > utc_datetime.hour:
					# retry it with new date (will have impact on daylight savings changes (but not sure it will actually help))
					currentDate = currentDate + timedelta(days=1)
				local_datetime = local_timezone.localize(datetime(currentDate.year, currentDate.month, currentDate.day, int(value)))
				utc_datetime = pytz.utc.normalize(local_datetime.astimezone(pytz.utc))
				self.hour = utc_datetime.hour
		if self.hour is not None and self.minute is not None:
			self.manager.addTrigger(self)

	def recalculate(self):
		if self.hour == -1:
			return False
		self.timezone = self.s.get('tz', 'UTC')
		currentHour = self.hour
		local_timezone = timezone(self.timezone)
		currentDate = pytz.utc.localize(datetime.utcnow())
		local_datetime = local_timezone.localize(datetime(currentDate.year, currentDate.month, currentDate.day, self.setHour))
		utc_datetime = pytz.utc.normalize(local_datetime.astimezone(pytz.utc))
		if datetime.now().hour > utc_datetime.hour:
			# retry it with new date (will have impact on daylight savings changes (but not sure it will actually help))
			currentDate = currentDate + timedelta(days=1)
		local_datetime = local_timezone.localize(datetime(currentDate.year, currentDate.month, currentDate.day, self.setHour))
		utc_datetime = pytz.utc.normalize(local_datetime.astimezone(pytz.utc))
		self.hour = utc_datetime.hour
		if currentHour == self.hour:
			#no change
			return False
		return True

class SuntimeTrigger(TimeTrigger):
	def __init__(self, manager, **kwargs):
		super(SuntimeTrigger,self).__init__(manager = manager, **kwargs)
		self.sunStatus = None
		self.offset = None
		self.latitude = self.s.get('latitude', '55.699592')
		self.longitude = self.s.get('longitude', '13.187836')

	def parseParam(self, name, value):
		if name == 'sunStatus':
			# rise = 1, set = 0
			self.sunStatus = int(value)
		elif name == 'offset':
			self.offset = int(value)
		if self.sunStatus is not None and self.offset is not None:
			self.recalculate()
			self.manager.addTrigger(self)

	def recalculate(self):
		self.latitude = self.s.get('latitude', '55.699592')
		self.longitude = self.s.get('longitude', '13.187836')
		sunCalc = SunCalculator()
		currentHour = self.hour
		currentMinute = self.minute
		currentDate = pytz.utc.localize(datetime.utcnow())
		riseSet = sunCalc.nextRiseSet(timegm(currentDate.utctimetuple()), float(self.latitude), float(self.longitude))
		if self.sunStatus == 0:
			runTime = riseSet['sunset']
		else:
			runTime = riseSet['sunrise']
		runTime = runTime + (self.offset*60)
		utc_datetime = datetime.utcfromtimestamp(runTime)

		tomorrow = currentDate + timedelta(days=1)
		if (utc_datetime.day != currentDate.day or utc_datetime.month != currentDate.month) and (utc_datetime.day != tomorrow.day or utc_datetime.month != tomorrow.month):
			# no sunrise/sunset today or tomorrow
			if self.active:
				self.active = False
				return True  # has changed (status to active)
			return False  # still not active, no change
		if currentMinute == utc_datetime.minute and currentHour == utc_datetime.hour and self.active:
			return False  # no changes
		self.active = True
		self.minute = utc_datetime.minute
		self.hour = utc_datetime.hour
		return True

class BlockheaterTrigger(TimeTrigger):
	def __init__(self, factory, manager, deviceManager, **kwargs):
		super(BlockheaterTrigger,self).__init__(manager = manager, **kwargs)
		self.factory = factory
		self.departureHour = None
		self.departureMinute = None
		self.sensorId = None
		self.temp = None
		self.deviceManager = deviceManager

	def close(self):
		self.factory.deleteTrigger(self)
		super(BlockheaterTrigger,self).close()

	def parseParam(self, name, value):
		if name == 'clientSensorId':
			self.sensorId = int(value)
		elif name == 'hour':
			self.departureHour = int(value)
		elif name == 'minute':
			self.departureMinute = int(value)
		if self.departureHour is not None and self.departureMinute is not None and self.sensorId is not None:
			self.recalculate()
			self.manager.addTrigger(self)

	def recalculate(self):
		if self.temp is None:
			if self.sensorId is None:
				return False
			sensor = self.deviceManager.device(self.sensorId)
			# TODO: Support Fahrenheit also
			temp = sensor.sensorValue(Device.TEMPERATURE, Device.SCALE_TEMPERATURE_CELCIUS)
			if temp is None:
				return False
			self.temp = temp
		if self.temp > 10:
			self.active = False
			return True
		self.active = True
		offset = int(round(60+100*self.temp/(self.temp-35)))
		offset = min(120, offset) #  Never longer than 120 minutes
		minutes = (self.departureHour * 60) + self.departureMinute - offset
		if minutes < 0:
			minutes += 24*60
		self.setHour = int(minutes / 60)
		self.minute = int(minutes % 60)
		return super(BlockheaterTrigger,self).recalculate()

	def setTemp(self, temp):
		self.temp = temp
		self.recalculate()

class SuntimeCondition(Condition):
	def __init__(self, **kwargs):
		super(SuntimeCondition,self).__init__(**kwargs)
		self.sunStatus = None
		self.sunriseOffset = None
		self.sunsetOffset = None
		self.s = Settings('telldus.scheduler')
		self.latitude = self.s.get('latitude', '55.699592')
		self.longitude = self.s.get('longitude', '13.187836')

	def parseParam(self, name, value):
		if name == 'sunStatus':
			self.sunStatus = int(value)
		if name == 'sunriseOffset':
			self.sunriseOffset = int(value)
		if name == 'sunsetOffset':
			self.sunsetOffset = int(value)

	def validate(self, success, failure):
		if self.sunStatus is None or self.sunriseOffset is None or self.sunsetOffset is None:
			# condition has not finished loading, impossible to validate it correctly
			failure()
			return
		sunCalc = SunCalculator()
		currentDate = pytz.utc.localize(datetime.utcnow())
		riseSet = sunCalc.nextRiseSet(timegm(currentDate.utctimetuple()), float(self.latitude), float(self.longitude))
		currentStatus = 1
		sunToday = sunCalc.riseset(timegm(currentDate.utctimetuple()), float(self.latitude), float(self.longitude))
		sunRise = None
		sunSet = None
		if sunToday['sunrise']:
			sunRise = sunToday['sunrise'] + (self.sunriseOffset*60)
		if sunToday['sunset']:
			sunSet = sunToday['sunset'] + (self.sunsetOffset*60)
		if sunRise or sunSet:
			if (sunRise and time.time() < sunRise) or (sunSet and time.time() > sunSet):
				currentStatus = 0
		else:
			# no sunset or sunrise, is it winter or summer?
			nextRiseSet = sunCalc.nextRiseSet(timegm(currentDate.utctimetuple()), float(self.latitude), float(self.longitude))
			if riseSet['sunrise'] < riseSet['sunset']:
				# next is a sunrise, it's dark now (winter)
				if time.time() < (riseSet['sunrise'] + (self.sunriseOffset*60)):
					currentStatus = 0
			else:
				# next is a sunset, it's light now (summer)
				if time.time() > (riseSet['sunset'] + (self.sunriseOffset*60)):
					currentStatus = 0
		if self.sunStatus == currentStatus:
			success()
		else:
			failure()

class TimeCondition(Condition):
	def __init__(self, **kwargs):
		super(TimeCondition,self).__init__(**kwargs)
		self.fromMinute = None
		self.fromHour = None
		self.toMinute = None
		self.toHour = None
		self.s = Settings('telldus.scheduler')
		self.timezone = self.s.get('tz', 'UTC')

	def parseParam(self, name, value):
		if name == 'fromMinute':
			self.fromMinute = int(value)
		elif name == 'toMinute':
			self.toMinute = int(value)
		elif name == 'fromHour':
			self.fromHour = int(value)
		elif name == 'toHour':
			self.toHour = int(value)

	def validate(self, success, failure):
		utcCurrentDate = pytz.utc.localize(datetime.utcnow())
		local_timezone = timezone(self.timezone)
		currentDate = utcCurrentDate.astimezone(local_timezone)
		if self.fromMinute is None or self.toMinute is None or self.fromHour is None or self.toHour is None:
			# validate that all parameters have been loaded
			failure()
			return
		toTime = local_timezone.localize(datetime(currentDate.year, currentDate.month, currentDate.day, self.toHour, self.toMinute, 0))
		fromTime = local_timezone.localize(datetime(currentDate.year, currentDate.month, currentDate.day, self.fromHour, self.fromMinute, 0))
		if fromTime > toTime:
			if (currentDate >= fromTime or currentDate <= toTime):
				success()
			else:
				failure()
		else:
			# condition interval passes midnight
			if (currentDate >= fromTime and currentDate <= toTime):
				success()
			else:
				failure()

class WeekdayCondition(Condition):
	def __init__(self, **kwargs):
		super(WeekdayCondition,self).__init__(**kwargs)
		self.weekdays = None
		self.s = Settings('telldus.scheduler')
		self.timezone = self.s.get('tz', 'UTC')

	def parseParam(self, name, value):
		if name == 'weekdays':
			self.weekdays = value

	def validate(self, success, failure):
		currentDate = pytz.utc.localize(datetime.utcnow())
		local_timezone = timezone(self.timezone)
		local_datetime = local_timezone.normalize(currentDate.astimezone(local_timezone))
		currentWeekday = local_datetime.weekday() + 1
		if str(currentWeekday) in self.weekdays:
			success()
		else:
			failure()

class SchedulerEventFactory(Plugin):
	implements(IEventFactory)
	implements(ISignalObserver)

	def __init__(self):
		self.triggerManager = TimeTriggerManager()
		self.blockheaterTriggers = []

	def clearAll(self):
		self.triggerManager.clearAll()

	def createCondition(self, type, params, **kwargs):
		if type == 'suntime':
			return SuntimeCondition(**kwargs)
		elif type == 'time':
			return TimeCondition(**kwargs)
		elif type == 'weekdays':
			return WeekdayCondition(**kwargs)

	def createTrigger(self, type, **kwargs):
		if type == 'blockheater':
			trigger = BlockheaterTrigger(factory=self, manager=self.triggerManager, deviceManager=DeviceManager(self.context), **kwargs)
			self.blockheaterTriggers.append(trigger)
			return trigger
		if type == 'time':
			trigger = TimeTrigger(manager=self.triggerManager, **kwargs)
			return trigger
		if type == 'suntime':
			trigger = SuntimeTrigger(manager=self.triggerManager, **kwargs)
			return trigger
		return None

	def deleteTrigger(self, trigger):
		if trigger in self.blockheaterTriggers:
			self.blockheaterTriggers.remove(trigger)

	def recalcTrigger(self):
		self.triggerManager.recalcAll()

	@slot('sensorValueUpdated')
	def sensorValueUpdated(self, device, valueType, value, scale):
		if valueType != Device.TEMPERATURE:
			return
		for trigger in self.blockheaterTriggers:
			if trigger.sensorId == device.id():
				trigger.setTemp(value)
				break
