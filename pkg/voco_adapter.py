"""Voco adapter for Mozilla WebThings Gateway."""

# A future release will no longer show privacy sensitive information via the debug option. 
# For now, during early development, it will be available. Please be considerate of others if you use this in a home situation.


from __future__ import print_function

import os
from os import path
import sys


import subprocess
from subprocess import call

sys.path.append(path.join(path.dirname(path.abspath(__file__)), 'lib'))


import json
import asyncio
import logging
import threading
import requests

#from types import SimpleNamespace
from collections import namedtuple

try:
    from types import SimpleNamespace as Namespace
except ImportError:
    # Python 2.x fallback
    from argparse import Namespace

import time
#from time import sleep
from datetime import datetime,timedelta
from dateutil import tz
from dateutil.parser import *

from subprocess import call, Popen
import queue

from .intentions import *

try:
    import paho.mqtt.publish as publish
    import paho.mqtt.client as client
except:
    print("ERROR, paho is not installed. try 'pip3 install paho'")

try:
    from rapidfuzz import fuzz
    from rapidfuzz import process
except:
    print("ERROR, rapidfuzz is not installed. try 'pip3 install rapidfuzz'")

try:
    import alsaaudio
except:
    print("ERROR, alsaaudio is not installed. try 'pip3 install alsaaudio'")

try:
    from pytz import timezone
    import pytz
except:
    print("ERROR, pytz is not installed. try 'pip3 install pytz'")

    
from gateway_addon import Database, Adapter
from .util import *
from .voco_device import *
from .voco_notifier import *





_TIMEOUT = 3

__location__ = os.path.realpath(
    os.path.join(os.getcwd(), os.path.dirname(__file__)))


class VocoAdapter(Adapter):
    """Adapter for Snips"""

    def __init__(self, verbose=True):
        """
        Initialize the object.
        
        verbose -- whether or not to enable verbose logging
        """
        print("initialising Voco")
        self.pairing = False
        self.DEBUG = True
        self.DEV = False
        self.addon_name = 'voco'
        self.name = self.__class__.__name__ # VocoAdapter
        #print("self.name = " + str(self.name))
        Adapter.__init__(self, self.addon_name, self.addon_name, verbose=verbose)
        #print("Adapter ID = " + self.get_id())

        os.environ["LD_LIBRARY_PATH"] = os.path.join(self.user_profile['baseDir'],'.mozilla-iot','addons','voco','snips') #"/home/pi/.mozilla-iot/addons/voco/snips/"


        # Get initial audio output options
        self.audio_controls = get_audio_controls()
        print(self.audio_controls)

        
        # Get persistent data
        try:
            self.persistence_file_path = os.path.join(self.user_profile['dataDir'], self.addon_name, 'persistence.json')
        except:
            try:
                print("setting persistence file path failed, will try older method.")
                self.persistence_file_path = os.path.join(os.path.expanduser('~'), '.mozilla-iot', 'data', 'voco','persistence.json')
            except:
                print("Double error making persistence file path")
                self.persistence_file_path = "/home/pi/.mozilla/data/voco/persistence.json"
        
        if self.DEBUG:
            print("Current working directory: " + str(os.getcwd()))
        
        first_run = False
        try:
            with open(self.persistence_file_path) as f:
                self.persistent_data = json.load(f)
                if self.DEBUG:
                    print("Persistence data was loaded succesfully.")
                    
                try:
                    self.persistent_data['listening'] = True
                    self.action_times = self.persistent_data['action_times']
                    
                    #try:
                    #    for index, item in enumerate(self.action_times):
                    #        if str(item['type']) == 'countdown':
                    #            print(str( item['moment'] ))
                    #            if int(item['moment']) > time.time():
                    #                self.countdown = int(item['moment'])
                    #                print("countdown restored, counting down to UTC: " + str(self.countdown))
                    #            else:
                    #                print("Countdown not restored as the target time was in the past")
                    #    
                    #except:
                    #    print("no countdown to restore")
                    
                    try:
                        if 'audio_output' not in self.persistent_data:
                            self.persistent_data['audio_output'] = str(self.audio_controls[0]['human_device_name'])
                    except:
                        print("Error fixing audio output in persistent data")
                        
                except:
                    print("self.action_times could not be loaded yet.")
                
        except:
            first_run = True
            print("Could not load persistent data (if you just installed the add-on then this is normal)")
            self.persistent_data = {'listening':True, 'feedback_sounds':True, 'speaker_volume':100, 'audio_output': str(self.audio_controls[0]['human_device_name'])}
            
        print("self.persistent_data is now: " + str(self.persistent_data))
            
        self.opposites = {
                "on":"off",
                "off":"on",
                "open":"closed",
                "closed":"open",
                "locked":"unlocked",
                "unlocked":"locked"
        }

        # Create a process group.
        #os.setpgrp()

        self.running = True

        # self.persistent_data is handled just above
        self.metric = True
        self.things = []
        self.token = None
        self.timer_counts = {'timer':0,'alarm':0,'reminder':0}
        self.temperature_unit = 'degrees celsius'

        self.action_times = [] # will hold all the timers
        self.countdown = int(time.time()) # There can only be one timer at a time. It's set the target unix time.
        
        self.server = 'http://127.0.0.1:8080' # will be replaced with https://127.0.0.1:4443 later on, if a test call to the api fails.
        self.mqtt_client = None

        # Microphone
        self.microphone = None
        self.capture_card_id = 1 # 0 is internal, 1 is usb.
        self.capture_device_id = 0 # Which channel
        self.capture_devices = []
        
        # Speaker
        self.speaker = None
        self.current_simple_card_name = "ALSA"
        self.current_card_id = 0
        self.current_device_id = 0

        # Snips settings
        self.external_processes = [] # Will hold all the spawned processes
        self.MQTT_IP_address = "localhost"
        self.MQTT_port = 1883
        self.snips_parts = ['snips-hotword','snips-asr','snips-tts','snips-audio-server','snips-nlu','snips-injection','snips-dialogue']
        self.snips_main_site_id = None
        self.custom_assistant_url = None
        self.larger_vocabulary_url = "https://raspbian.snips.ai/stretch/pool/s/sn/snips-asr-model-en-500MB_0.6.0-alpha.4_armhf.deb"
        #self.h = None # will hold the Hermes object, which is used to communicate with Snips
        self.pleasantry_count = 0 # How often Snips has heard "please". Will be used to thank the use for being cordial once in a while.
        self.hotword_process = None
        self.intent_received = True # Used to create a 'no voice input received' sound effect if no intent was heard.
        
        # Voice settings
        self.voice_accent = "en-GB"
        self.voice_pitch = "1.2"
        self.voice_speed = "0.9"
        
        # These will be injected ino Snips for better recognition.
        self.extra_properties = ["state","set point"]
        self.generic_properties = ["level","value","values","state","states","all values","all levels"]
        self.capabilities = ["temperature"]
        self.numeric_property_names = ["first","second","third","fourth","fifth","sixth","seventh"]
         
        # Time
        #self.time_zone = "Europe/Amsterdam"
        self.time_zone = str(time.tzname[0])
        self.seconds_offset_from_utc = 7200 # Used for quick calculations when dealing with timezones.
        self.last_injection_time = time.time() #datetime.utcnow().timestamp() #0 # The last time the things/property names list was sent to Snips.
        self.minimum_injection_interval = 15  # Minimum amount of seconds between new thing/property name injection attempts.
        self.attempting_injection = False
        self.current_utc_time = 0
        
        # Some paths
        self.addon_path =  os.path.join(os.path.expanduser('~'), '.mozilla-iot', 'addons', 'voco')
        self.snips_path = os.path.join(self.addon_path,"snips")
        self.arm_libs_path = os.path.join(self.snips_path,"arm-linux-gnueabihf")
        self.assistant_path = os.path.join(self.snips_path,"assistant")
        self.work_path = os.path.join(self.snips_path,"work")
        self.toml_path = os.path.join(self.snips_path,"snips.toml")
        self.hotword_path = os.path.join(self.snips_path,"snips-hotword")
        self.mosquitto_path = os.path.join(self.snips_path,"mosquitto")
        self.g2p_models_path = os.path.join(self.snips_path,"g2p-models")
        self.start_of_input_sound = os.path.join(self.addon_path,"sounds","start_of_input.wav")
        self.end_of_input_sound = os.path.join(self.addon_path,"sounds","end_of_input.wav")
        self.alarm_sound = os.path.join(self.addon_path,"sounds","alarm.wav")
        self.error_sound = os.path.join(self.addon_path,"sounds","error.wav")


        # Make sure the work directory exists
        try:
            if not os.path.isdir(self.work_path):
                os.mkdir( self.work_path )
                print("Work directory did not exist, created it now")
        except:
            print("Error: could not make sure work dir exists. Work path: " + str(self.work_path))
            

        
        # create list of human readable audio-only output options for thing property
        self.audio_output_options = []
        for option in self.audio_controls:
            self.audio_output_options.append( option['human_device_name'] )

        if self.DEBUG:
            print("self.audio_output_options = " + str(self.audio_output_options))
        
        
        # Create Voco device
        try:
            voco_device = VocoDevice(self, self.audio_output_options)
            self.handle_device_added(voco_device)
            if self.DEBUG:
                print("Voco thing created")
        except Exception as ex:
            print("Could not create voco device:" + str(ex))


        # Stop Snips until the init is complete (if it is installed).
        try:
            os.system("pkill -f snips") # Avoid snips running paralel
            self.devices['voco'].connected = False
            self.devices['voco'].connected_notify(False)
        except Exception as ex:
            print("Could not stop Snips: " + str(ex))
            
            
        if self.DEBUG:
            print("available audio cards: " + str(alsaaudio.cards()))
        
            
        # Pre-scan ALSA
        try:
            self.capture_devices = self.scan_alsa('capture')
            print("Possible audio capture devices: " + str(self.capture_devices))
            
        except Exception as ex:
            print("Error scanning ALSA (audio devices): " + str(ex))
        
        
        # Install Snips if it hasn't been installed already
        try:
            self.snips_installed = self.install_snips()
        except Exception as ex:
            print("Error while trying to install Snips/check if Snips should be installed: " + str(ex))
            
            
        # LOAD CONFIG
        
        try:
            self.add_from_config()
        except Exception as ex:
            print("Error loading config: " + str(ex))
            
        # Get all the things via the API.
        try:
            self.things = self.api_get("/things")
            #if self.DEBUG:
            #    print("Did the initial API call to /things. Result: " + str(self.things))
            try:
                if self.things['error'] == '403':
                    if self.DEBUG:
                        print("Spotted 403 error, will try to switch to https API calls")
                    self.server = 'https://127.0.0.1:4443'
                    self.things = self.api_get("/things")
                    if self.DEBUG:
                        print("Tried the API call again, this time at port 4443. Result: " + str(self.things))
            except Exception as ex:
                pass
                #print("Error handling API: " + str(ex))
                
        except Exception as ex:
            print("Error, couldn't load things at init: " + str(ex))

        print("self.server is now: " + str(self.server))

            
        # AUDIO

        # Fix the audio input.
        if self.microphone == "Built-in microphone (0,0)":
            print("Setting audio input to built-in")
            self.capture_card_id = 0
            self.capture_device_id = 0
        elif self.microphone == "Attached device (1,0)":
            print("Setting audio input to USB/Hat")
            self.capture_card_id = 1
            self.capture_device_id = 0
        elif self.microphone == "ReSpeaker (2,0)":
            print("Setting audio input to ReSpeaker")
            self.capture_card_id = 2
            self.capture_device_id = 0

        # Fix the audio output. The default on the WebThings image is HDMI.
        if self.speaker == "Headphone jack":
            print("Setting audio output to headphone jack")
            run_command("amixer cset numid=3 1")
        elif self.speaker == "HDMI":
            print("Setting audio output to HDMI")
            run_command("amixer cset numid=3 2")
        elif self.speaker == "Auto":
            print("Setting audio output to automatically switch")
            run_command("amixer cset numid=3 0")
            
        # Get the initial speaker settings
        for option in self.audio_controls:
            if option['human_device_name'] == str(self.persistent_data['audio_output']):
                self.current_simple_card_name = option['simple_card_name']
                self.current_card_id = option['card_id']
                self.current_device_id = option['device_id']
            
        
        # TIME
        
        # Calculate timezone difference between the user set timezone and UTC.
        try:
            self.user_timezone = timezone(self.time_zone)
            
            #utcnow = datetime.now(tz=pytz.utc)
            #usernow = self.user_timezone.localize(datetime.utcnow()) # utcnow() is naive
            
            #print("The universal time is " + str(utcnow))
            #print("Simpler, time.time() is: " + str( time.time() ))
            #print("In " + str(self.time_zone) + " the current time is " + str(usernow))
            #print("With your current localization settings, your computer will tell you it is now " + str(now))

            #tdelta = utcnow - usernow
            #self.seconds_offset_from_utc = round(tdelta.total_seconds())
            #print("The difference between UTC and user selected timezone, in seconds, is " + str(self.seconds_offset_from_utc))
            self.seconds_offset_from_utc = (time.timezone if (time.localtime().tm_isdst == 0) else time.altzone) * -1
            if self.DEBUG:
                print("Simpler timezone offset in seconds = " + str(self.seconds_offset_from_utc))
            
        except Exception as ex:
            print("Error handling time zone calculation: " + str(ex))
            
        if self.DEBUG:
            print("Starting the Snips processes in a thread")
        try:
            p = threading.Thread(target=self.run_snips)
            p.daemon = True
            p.start()
        except:
            print("Error starting the run_snips thread")
            
        time.sleep(1.17)
            
            
        # Create notifier
        
        self.voice_messages_queue = queue.Queue()
        self.notifier = VocoNotifier(self,self.voice_messages_queue,verbose=True) # TODO: It could be nice to move speech completely to a queue system so that voice never overlaps.


        # Start the internal clock which is used to handle timers. It also receives messages from the notifier.
        print("Starting the internal clock")
        try:
            # Restore the timers, alarms and reminders from persistence.
            if 'action_times' in self.persistent_data:
                print("loading action times from persistence") 
                self.action_times = self.persistent_data['action_times']
            
            t = threading.Thread(target=self.clock, args=(self.voice_messages_queue,))
            t.daemon = True
            t.start()
        except:
            print("Error starting the clock thread")


            
        time.sleep(3.14)

        
        # Set the correct speaker volume
        try:
            print("Speaker volume from persistence was: " + str(self.persistent_data['speaker_volume']))
            self.set_speaker_volume(self.persistent_data['speaker_volume'])
        except Exception as ex:
            print("Could not set initial audio volume: " + str(ex))
        

        try:
            self.devices['voco'].connected = True
            self.devices['voco'].connected_notify(True)
            self.set_status_on_thing("Listening")
        except Exception as ex:
            print("Error setting device details: " + str(ex))
            
        # Let's try again.
        try:
            self.update_timer_counts()
        except:
            print("Error resetting timer counts")
        
        time.sleep(5.4) # Snips needs some time to start
        
        #if self.persistent_data['listening'] == True:
        self.speak("Hello, I am Snips. ")
    
        if first_run:
            time.sleep(.5)
            self.speak("If you would like to ask me something, say. Hey Snips. ")
        
        if self.token == None:
            time.sleep(1)
            print("PLEASE ENTER YOUR AUTHORIZATION CODE IN THE SETTINGS PAGE")
            self.set_status_on_thing("Authorization code missing, check settings")
            self.speak("I cannot connect to your devices because the authorization code is missing. Check the voco settings page for details.")
            
        try:
            self.mqtt_client = client.Client(client_id="voco_mqtt_snips_client")
            HOST = "localhost"
            PORT = 1883
            self.mqtt_client.on_connect = self.on_connect
            self.mqtt_client.on_message = self.on_message
            self.mqtt_client.connect(HOST, PORT) #, keepalive=60)
            #self.mqtt_client.loop_forever()
            self.mqtt_client.loop_start()
            print("Voco MQTT client started")
        except Exception as ex:
            print("Error creating extra MQTT connection: " + str(ex))

        print("")




#
#  GET CONFIG
#

    # Read the settings from the add-on settings page
    def add_from_config(self):
        """Attempt to add all configured devices."""
        try:
            database = Database('voco')
            if not database.open():
                print("Could not open settings database")
                return
            
            config = database.load_config()
            database.close()
            
        except:
            print("Error! Failed to open settings database.")
        
        if not config:
            print("Error loading config from database")
            return
        
        if self.DEV:
            print(str(config))

        if 'Debugging' in config:
            print("-Debugging was in config")
            self.DEBUG = bool(config['Debugging'])
            if self.DEBUG:
                print("Debugging enabled")

        try:
            store_updated_settings = False
            if 'Microphone' in config:
                print("-Microphone is present in the config data: " + str(config['Microphone']))
                
                if len(self.capture_devices) == 0 or str(config['Microphone']) in self.capture_devices:
                    print("--Using microphone from config")
                    self.microphone = str(config['Microphone'])         # If the prefered device in config also exists in hardware, then select it.
                else:
                    print("--Overriding the selected microphone because that device did not actually exist/was not plugged in.")
                    config['Microphone'] = self.capture_devices[0]      # If the prefered device in config does not actually exist, but the scan did sho connected hardware, then select the first item from the scan results instead.
                    self.microphone = self.capture_devices[0]
                    store_updated_settings = True
                
            if 'Speaker' in config:
                print("-Speaker is present in the config data: " + str(config['Speaker']))
                self.speaker = str(config['Speaker'])               # If the prefered device in config also exists in hardware, then select it.

        except:
            print("Error loading microphone settings")


        try:
            should_install = False

            # if assistant.json is not in the assistant folder.
            if not os.path.isdir( os.path.join(self.addon_path,"snips","assistant")):
                should_install = True

            # Install assistant if it hasn't been installed already
            if should_install:
                try:
                    self.assistant_installed = self.install_assistant()
                except Exception as ex:
                    print("Error while trying to install assistant/check if should be installed: " + str(ex))     
        except:
            print("Error starting snips install process from settings")
               
               
        try:
            # Store the settings that were changed by the add-on.
            if store_updated_settings:
                print("Storing overridden settings")

                database = Database('voco')
                if not database.open():
                    print("Error, could not open settings database")
                    #return
                else:
                    database.save_config(config)
                    database.close()
                    if self.DEBUG:
                        print("Stored overridden preferences into the database")
        except:
            print("Error! Failed to store overridden settings in database.")
            
            
            
        # Time zone
        try:
            if 'Voice accent' in config:
                print("-Voice accent is present in the config data.")
                self.voice_accent = str(config['Voice accent'])
            if 'Voice pitch' in config:
                print("-Voice pitch is present in the config data.")
                self.voice_pitch = str(config['Voice pitch'])
            if 'Voice speed' in config:
                print("-Voice speed is present in the config data.")
                self.voice_speed = str(config['Voice speed']) 
                
        except Exception as ex:
            print("Error loading voice setting(s) from config: " + str(ex))
                
              
        # Metric or Imperial
        try:   
            if 'Metric' in config:
                print("-Metric preference is present in the config data.")
                self.metric = bool(config['Metric'])
                if self.metric == False:
                    self.temperature_unit = 'degrees fahrenheit'
            else:
                self.metric = True
        except Exception as ex:
            print("Error loading locale information from config: " + str(ex))
            
            
        # Api token
        try:
            if 'Authorization token' in config:
                self.token = str(config['Authorization token'])
                print("-Authorization token is present in the config data.")
        except:
            print("Error loading api token from settings")


        # Speaker volume
        try:
            if 'Speaker volume' in config:
                print("-Speaker volume is present in the config data")
                if 'speaker_volume' not in self.persistent_data:
                    print("There was no volume level set in persistent data")
                    self.persistent_data['speaker_volume'] = int(config['Speaker volume'])
                else:
                    print("--Using audio volume from persistent data")

        except Exception as ex:
            print("Error, couldn't get volume level: " + str(ex))




#
#  AUDIO
#


    def scan_alsa(self,device_type):
        """ Checks what audio hardware is available """
        result = []
        try:
            if device_type == "playback":
                command = "aplay -l"
            if device_type == "capture":
                command = "arecord -l"
                
            for line in run_command_with_lines(command):
                #print(str(line))
                
                if line.startswith('card 0'):
                    if 'device 0' in line:
                        if device_type == 'playback':
                            result.append('Built-in headphone jack (0,0)')
                        if device_type == 'capture':
                            result.append('Built-in microphone (0,0)')
                    elif 'device 1' in line:
                        if device_type == 'playback':
                            result.append('Built-in HDMI (0,1)')
                        if device_type == 'capture':
                            result.append('Built-in microphone, channel 2 (0,1)')
                            
                if line.startswith('card 1'):
                    if 'device 0' in line:
                        result.append('Attached device (1,0)')
                    #elif 'device 1' in line:
                    #    if device_type == 'playback':
                    #        result.append('Plugged-in (USB) device, channel 2 (1,1)')
                    #    if device_type == 'capture':
                    #        result.append('Plugged-in (USB) microphone, channel 2 (1,1)')
                            
                if line.startswith('card 2'):
                    if 'device 0' in line:
                        result.append('Second attached device (2,0)')
                    elif 'device 1' in line:
                        result.append('Second attached device, channel 2 (2,1)')
                            
        except Exception as e:
            print("Error during ALSA scan: " + str(e))
        return result



    def set_speaker_volume(self, volume):
        if self.DEBUG:
            print("in set_speaker_volume with " + str(volume))
        if volume != self.persistent_data['speaker_volume']:
            self.persistent_data['speaker_volume'] = int(volume)
            self.save_persistent_data()
            try:
                self.devices['voco'].properties['volume'].update(int(volume))
            except:
                if self.DEBUG:
                    print("error setting volume property on thing")
                    


    # Called by user to change audio output
    def set_audio_output(self, selection):
        if self.DEBUG:
            print("Setting audio output selection to: " + str(selection))
            
        # Get the latest audio controls
        self.audio_controls = get_audio_controls()
        print(self.audio_controls)
        
        try:        
            for option in self.audio_controls:
                if str(option['human_device_name']) == str(selection):
                    
                    self.current_simple_card_name = option['simple_card_name']
                    self.current_card_id = option['card_id']
                    self.current_device_id = option['device_id']
                    
                    # Set selection in persistence data
                    self.persistent_data['audio_output'] = str(selection)
                    self.save_persistent_data()
                    
                    if self.DEBUG:
                        print("new output selection on thing: " + str(selection))
                    try:
                        if self.DEBUG:
                            print("self.devices = " + str(self.devices))
                        if self.devices['voco'] != None:
                            self.devices['voco'].properties['audio output'].update( str(selection) )
                    except Exception as ex:
                        print("Error setting new audio output selection:" + str(ex))
        
                    break
            
        except Exception as ex:
            print("Error in set_audio_output: " + str(ex))



    def play_sound(self,sound_file):
        #os.system("aplay " + str(sound_file) + " -D plughw:" + str(self.current_card_id) + "," + str(self.current_device_id))
        sound_file = os.path.splitext(sound_file)[0] + str(self.persistent_data['speaker_volume']) + '.wav'
        
        #os.system("aplay " + str(sound_file) + str(self.persistent_data['speaker_volume']) + " -D plughw:" + str(self.current_card_id) + "," + str(self.current_device_id))
        
        os.system("aplay " + str(sound_file) + " -D plughw:" + str(self.current_card_id) + "," + str(self.current_device_id))
        #os.system("aplay " + str(sound_file) + " -D dmix:CARD=" + str(self.current_simple_card_name) + ",DEV=" + str(self.current_device_id))
        
        try:
            #environment = {}
            for option in self.audio_controls:
                if option['human_device_name'] == str(self.persistent_data['audio_output']):
                    #environment["ALSA_CARD"] = str(option['simple_card_name'])
                    pass
        
                    #try:
                    #    if self.sound_player != None:
                    #        self.sound_player.terminate()
                    #except:
                    #    if self.DEBUG:
                    #        print("Sound player process did not exist yet")
                    
                    #sound_command = 'ALSA_CARD=plughw:' + str(self.current_card_id) + ',' + str(self.current_device_id) +  ' ffplay -nodisp -vn -infbuf -autoexit -v 0 -volume ' + str(self.persistent_data['speaker_volume']) + ' ' +  str(sound_file)
                    #sound_command = ("ffplay", "-nodisp", "-vn", "-infbuf","-autoexit", "-v","0","-volume",str(self.persistent_data['speaker_volume']), str(sound_file) )
                    #if self.DEBUG:
                        #print("environment = " + str(environment))
                    #    print("sound_command = " + str(sound_command))
                    #    print("playing sound file: " + str(sound_file))
                    #self.sound_player = subprocess.Popen(sound_command, 
                    #                env=environment,
                    #                stdin=subprocess.PIPE,
                    #                stdout=subprocess.PIPE,
                    #                stderr=subprocess.PIPE)
        except Exception as ex:
            print("Error playing sound: " + str(ex))



    def speak(self, voice_message="",site_id="default"):
        try:
            # TODO create a queue?

            environment = os.environ.copy()
            FNULL = open(os.devnull, 'w')
            
            for option in self.audio_controls:
                if option['human_device_name'] == str(self.persistent_data['audio_output']):
                    environment["ALSA_CARD"] = str(option['simple_card_name'])
                    #if self.DEBUG:
                    #    print("environment = " + str(environment))
            
            
                    try:
                        if self.nanotts_process != None:
                            self.nanotts_process.terminate()
                    except:
                        if self.DEBUG:
                            print("nanotts_process did not exist yet")
                    
                    #my_env = os.environ.copy()
                    #my_env["ALSA_CARD"] = str(self.current_card_id)
                    #print('my_env["ALSA_CARD"] = ' + str(my_env["ALSA_CARD"]))
            
                   
            
                    nanotts_volume = int(self.persistent_data['speaker_volume']) / 100
            
                    if self.DEBUG:
                        print("nanotts_volume = " + str(nanotts_volume))
            
                    #nanotts_command = (str(os.path.join(self.snips_path,'nanotts')), '-l',str(os.path.join(self.snips_path,'lang')),'-v',str(self.voice_accent),'--volume',str(nanotts_volume),'--speed',str(self.voice_speed),'--pitch',str(self.voice_pitch),'-p')
            
                    #if self.DEBUG:
                    #    print("nanotts_command = " + str(nanotts_command))
            
            
                    self.nanotts_process = subprocess.Popen(('echo', str(voice_message)), stdout=subprocess.PIPE)
                    output = subprocess.check_output((str(os.path.join(self.snips_path,'nanotts')),'-l',str(os.path.join(self.snips_path,'lang')),'-v',str(self.voice_accent),'--volume',str(nanotts_volume),'--speed',str(self.voice_speed),'--pitch',str(self.voice_pitch),'-p'), stdin=self.nanotts_process.stdout, env=environment)
                    self.nanotts_process.wait()
                    
                    #ps = subprocess.Popen(('echo', str(voice_message)), stdout=subprocess.PIPE)
                    #output = subprocess.check_output((str(os.path.join(self.snips_path,'nanotts')), '-l',str(os.path.join(self.snips_path,'lang')),'-v',str(self.voice),'--volume',nanotts_volume,'--speed','0.9','--pitch','1.2','-p'), stdin=ps.stdout, env=my_env)
                    #ps.wait()
            
                    #self.nanotts_process = subprocess.Popen(nanotts_command, 
                    #                env=environment,
                    #                stdin=subprocess.PIPE,
                    #                stdout=subprocess.PIPE,
                    #                stderr=subprocess.PIPE)
            
        except Exception as ex:
            print("Error speaking: " + str(ex))














#
#  RUN SNIPS
#


    def run_snips(self):
        
        try:
            time.sleep(1.11)
            #self.play_sound(self.end_of_input_sound)
        
            commands = [
                'snips-tts',
                'snips-audio-server',
                'snips-dialogue',
                'snips-asr',
                'snips-nlu',
                'snips-injection'
            ]
        
            my_env = os.environ.copy()
            my_env["LD_LIBRARY_PATH"] = '{}:{}'.format(self.snips_path,self.arm_libs_path)
        
            #FNULL = open(os.devnull, 'w')
            
            if self.DEV:
                print("--my_env = " + str(my_env))
        
            #print("starting mosquitto")
            #mosquitto_command = [self.mosquitto_path,"-d"] # -d for daemon
            #mosquitto_command = [self.mosquitto_path] # -d for daemon
            #self.mosquitto_process = Popen(mosquitto_command, env=my_env) # Mosquitto is now a default part of the Mozilla WebThings Gateway.

            #sleep(3) # Give mosquitto some time to start
            #print("-- 3 seconds")
            #self.play_sound(self.end_of_input_sound)
        
            # Start the snips parts
            for unique_command in commands:
                #print("")
                #command = self.generate_normal_process_command(str(unique_command))
                bin_path = os.path.join(self.snips_path,unique_command)
                command = [bin_path,"-u",self.work_path,"-a",self.assistant_path,"-c",self.toml_path]
                if unique_command == 'snips-audio-server':
                    #command = command + ["--alsa_capture","plughw:" + str(self.capture_card_id) + "," + str(self.capture_device_id),"--alsa_playback","default:" + str(self.current_card_id) + "," + str(self.current_device_id)]
                    command = command + ["--alsa_capture","plughw:" + str(self.capture_card_id) + "," + str(self.capture_device_id),"--alsa_playback","default:CARD=ALSA"]
                    
                
                elif unique_command == 'snips-injection':
                    command = command + ["-g",self.g2p_models_path]
                #elif unique_command == 'snips-asr':
                #    command = command + ["--thread_number","1"] # TODO Check if this actually helps.
            
                #command = command + ["&>","/dev/null"] #&> /dev/null
                
                #if self.DEV:
                if self.DEBUG:
                    print("--generated command = " + str(command))
                
                self.external_processes.append( Popen(command, env=my_env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT) )
                #retcode = subprocess.call(['echo', 'foo'], stdout=FNULL, stderr=subprocess.STDOUT)
                
                time.sleep(1)
                if self.DEBUG:
                    print("-- waiting 1 seconds in Snips startup loop")
                #self.play_sound(self.end_of_input_sound)
            
            #if self.persistent_data['listening'] == True:
            if self.hotword_process == None:
                #if self.persistent_data['listening'] == True:
                
                hotword_command = [self.hotword_path,"-u",self.work_path,"-a",self.assistant_path,"-c",self.toml_path]
                if self.DEBUG:
                    print("hotword_command = " + str(hotword_command))
                self.hotword_process = Popen(hotword_command, env=my_env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

                # Reflect the state of Snips on the thing
                try:
                    #self.devices['voco'].properties['listening'].update( bool(self.persistent_data['listening']) )
                    self.devices['voco'].properties['listening'].update( True )
                    if self.token != None:
                        self.set_status_on_thing("Listening")
                except Exception as ex:
                    print("Error while setting the state on the thing: " + str(ex))
                        
                #else:
                #    # Reflect the state of Snips on the thing
                #    try:
                #        self.devices['voco'].properties['listening'].update( bool(self.persistent_data['listening']) )
                #        self.set_status_on_thing("Stopped")
                #    except Exception as ex:
                #        print("Error while setting the state on the thing: " + str(ex))
                
               
        except Exception as ex:
            print("Error starting Mosquitto/Snips processes: " + str(ex))    
         
        #for p in self.external_processes: 
        #    p.wait()
        #    print("Waiting...")
        
        #self.play_sound(self.end_of_input_sound)
        
        quick_counter = 0
        while self.mqtt_client == None:
            time.sleep(1)
            quick_counter += 1
            if quick_counter == 15:
                break
        
        #try:
        #    self.inject_updated_things_into_snips(True) # Check if there are new things/properties that Snips should learn about
        #except Exception as ex:
        #    print("Error, couldn't teach Snips the names of your things: " + str(ex))  
        
        return
        


    def install_snips(self):
        """Install Snips using a shell command"""

        try:
            if os.path.isdir(self.snips_path):
                print("Snips has already been extracted")
                return True
            
            else:
                print("It seems Snips hasn't been extracted yet - snips directory could not be found..")
                
                command = "tar xzf " + str(os.path.join(self.addon_path,"snips.tar")) + " --directory " + str(self.addon_path)
                #print("Snips install command: " + str(command))
                self.set_status_on_thing("Unpacking Snips")
                if self.DEBUG:
                    print("Snips install command: " + str(command))
                if run_command(command) == 0:
                    print("Succesfully extracted")
                else:
                    print("Error in call to extract")
                        
        except Exception as ex:
            self.set_status_on_thing("Error during Snips installation")
            print("Error in Snips installation: " + str(ex))
            
        return False





    def install_assistant(self):
        """Install snips/assistant.zip into snips/assistant directory"""
        
        print("Installing assistant")
        try:
            if not os.path.isfile( os.path.join(self.addon_path,"snips","assistant.zip") ):
                print("Error: cannot install assistant: there doesn't seem to be an assistant.zip file in the snips folder of the addon.")
                return False
            command = "unzip -o assistant.zip"
            if run_command(command, cwd=os.path.join(self.addon_path,"snips")) == 0:
                self.set_status_on_thing("Succesfully installed assistant")
                return True
            else:
                self.set_status_on_thing("Error installing assistant")
                return False
        except Exception as ex:
            print("installing assistant: error: " + str(ex))
        return False











#
#  CLOCK
#

    def clock(self, voice_messages_queue):
        """ Runs every second and handles the various timers """
        previous_action_times_count = 0
        #previous_injection_time = time.time()
        while self.running:

            # Inject new thing names into snips if necessary
            #if datetime.utcnow().timestamp() - self.minimum_injection_interval > self.last_injection_time: # + self.minimum_injection_interval > datetime.utcnow().timestamp():
            if time.time() - self.minimum_injection_interval > self.last_injection_time: # + self.minimum_injection_interval > datetime.utcnow().timestamp():
                self.last_injection_time = time.time() #datetime.utcnow().timestamp()
                #previous_injection_time = time.time()
                self.inject_updated_things_into_snips()
                

            voice_message = ""
            #utcnow = int(time.time()) #datetime.now(tz=pytz.utc)
            fresh_time = int(time.time()) #int(utcnow.timestamp())
            
            if fresh_time == self.current_utc_time:
                time.sleep(.1)
            else:
                self.current_utc_time = fresh_time
                #timer_removed = False
                try:
                    
                    
                    
                    #print(str(self.current_utc_time))

                    for index, item in enumerate(self.action_times):
                        print("timer item = " + str(item))

                        try:
                            # Wake up alarm
                            if item['type'] == 'wake' and self.current_utc_time >= int(item['moment']):
                                if self.DEBUG:
                                    print("(...) WAKE UP")
                                #timer_removed = True
                                self.play_sound(self.alarm_sound)
                                self.speak("Good morning, it's time to wake up.")

                            # Normal alarm
                            elif item['type'] == 'alarm' and self.current_utc_time >= int(item['moment']):
                                if self.DEBUG:
                                    print("(...) ALARM")
                                self.play_sound(self.alarm_sound)
                                self.speak("This is your alarm notification")

                            # Reminder
                            elif item['type'] == 'reminder' and self.current_utc_time >= int(item['moment']):
                                if self.DEBUG:
                                    print("(...) REMINDER")
                                self.play_sound(self.end_of_input_sound)
                                voice_message = "This is a reminder to " + str(item['reminder_text'])
                                self.speak(voice_message)

                            # Delayed setting of a boolean state
                            elif item['type'] == 'actuator' and self.current_utc_time >= int(item['moment']):
                                if self.DEBUG:
                                    print("origval:" + str(item['original_value']))
                                    print("(...) TIMED ACTUATOR SWITCHING")
                                #delayed_action = True
                                intent_set_state(self, item['slots'],None, item['original_value'])

                            # Delayed setting of a value
                            elif item['type'] == 'value' and self.current_utc_time >= int(item['moment']):
                                if self.DEBUG:
                                    print("origval:" + str(item['original_value']))
                                    print("(...) TIMED SETTING OF A VALUE")
                                intent_set_value(self, item['slots'],None, item['original_value'])

                            # Countdown
                            elif item['type'] == 'countdown':
                                print("in countdown type")
                                try:
                                    if int(item['moment']) >= int(self.current_utc_time): # This one is reversed - it's only trigger as long as it hasn't reached the target time.
                                        
                                        #countdown_delta = self.countdown - self.current_utc_time
                                        countdown_delta = int(item['moment']) - self.current_utc_time
                                        
                                        print("Countdown delta:" + str(countdown_delta))   
                                        # Update the countdown on the voco thing
                                        if countdown_delta > 0:
                                            self.devices['voco'].properties[ 'countdown' ].set_cached_value_and_notify( int(countdown_delta) )
                                        else:    
                                            self.devices['voco'].properties[ 'countdown' ].set_cached_value_and_notify( 0 )
                                        
                                        # Create speakable countdown message
                                        if countdown_delta > 86400:
                                            if countdown_delta % 86400 == 0:

                                                days_to_go = countdown_delta//86400
                                                if days_to_go > 1:
                                                    voice_message = "countdown has " + str(days_to_go) + " days to go"
                                                else:
                                                    voice_message = "countdown has " + str(days_to_go) + " day to go"

                                        elif countdown_delta > 3599:
                                            if countdown_delta % 3600 == 0:

                                                hours_to_go = countdown_delta//3600
                                                if hours_to_go > 1:
                                                    voice_message = "countdown has " + str(hours_to_go) + " hours to go"
                                                else:
                                                    voice_message = "countdown has " + str(hours_to_go) + " hour to go"

                                        elif countdown_delta > 59:
                                            if countdown_delta % 60 == 0:

                                                minutes_to_go = countdown_delta//60
                                                if minutes_to_go > 1:
                                                    voice_message = "countdown has " + str(minutes_to_go) + " minutes to go"
                                                else:
                                                    voice_message = "countdown has " + str(minutes_to_go) + " minute to go"

                                        elif countdown_delta == 30:
                                            voice_message = "Counting down 30 seconds"

                                        elif countdown_delta > 0 and countdown_delta < 11:
                                            voice_message = str(int(countdown_delta))

                                        elif countdown_delta < 0:
                                            print("countdown delta was negative, strange.")
                                            del self.action_times[index]
                                            self.save_persistent_data()
                                        
                                        if voice_message != "":
                                            if self.DEBUG:
                                                print("(...) " + str(voice_message))
                                            self.speak(voice_message)
                                    else:
                                        print("removing countdown item")
                                        del self.action_times[index]
                                except Exception as ex:
                                    print("Error updating countdown: " + str(ex))

                            # Anything without a type will be treated as a normal timer.
                            elif self.current_utc_time >= int(item['moment']):
                                self.play_sound(self.end_of_input_sound)
                                if self.DEBUG:
                                    print("(...) Your timer is finished")
                                self.speak("Your timer is finished")

                        except Exception as ex:
                            print("Clock: error recreating event from timer: " + str(ex))
                            # TODO: currently if this fails it seems the timer item will stay in the list indefinately. If it fails, it should still be removed.

                    # Remove timers whose time has come 
                    try:
                        timer_removed = False
                        for index, item in enumerate(self.action_times):
                            #print(str(self.current_utc_time) + " ==?== " + str(int(item['moment'])))
                            if int(item['moment']) <= self.current_utc_time:
                                timer_removed = True
                                if self.DEBUG:
                                    print("removing timer from list")
                                del self.action_times[index]
                        if timer_removed:
                            if self.DEBUG:
                                print("at least one timer was removed")
                                self.save_persistent_data()
                    except Exception as ex:
                        print("Error while removing old timers: " + str(ex))

                except Exception as ex:
                    print("Clock error: " + str(ex))


                # Check if anything from the notifier should be spoken
                try:
                    notifier_message = voice_messages_queue.get(False)
                    if notifier_message != None:
                        if self.DEBUG:
                            print("Incoming message from notifier: " + str(notifier_message))
                        self.speak(str(notifier_message))
                except:
                    pass

                # Update the persistence data if the number of timers has changed
                try:
                    if len(self.action_times) != previous_action_times_count:
                        if self.DEBUG:
                            print("New total amount of reminders+alarms+timers+countdown: " + str(len(self.action_times)))
                        previous_action_times_count = len(self.action_times)
                        self.update_timer_counts()
                        self.persistent_data['action_times'] = self.action_times
                        self.save_persistent_data()
                except Exception as ex:
                    print("Error updating timer counts from clock: " + str(ex))



#
#  THINGS PROPERTIES
#

    def set_status_on_thing(self,status_string):
        """Set a string to the status property of the snips thing """
        if self.DEBUG:
            print("Setting status on thing to: " +str(status_string))
        try:
            if self.devices['voco'] != None:
                self.devices['voco'].properties['status'].update( str(status_string) )
        except:
            print("Error setting status of voco device")



    # Count how many timers, alarms and reminders have now been set, and update the voco device
    def update_timer_counts(self):
        try:
            self.timer_counts = {'timer':0,'alarm':0,'reminder':0}
            countdown_active = False
            for index, item in enumerate(self.action_times):
                current_type = item['type']
                #print(str(current_type))
                if current_type == "countdown":
                    #print("Spotted a countdown object")
                    countdown_active = True
                if current_type == "wake":
                    current_type = "alarm"
                if current_type == "actuator" or current_type == "value":
                    current_type = "timer"
                if current_type in self.timer_counts:
                    self.timer_counts[current_type] += 1
            
            if self.DEBUG:
                if self.DEBUG:print("updated timer counts = " + str(self.timer_counts))

            if countdown_active == False:
                self.devices['voco'].properties[ 'countdown' ].set_cached_value_and_notify( 0 )

            for timer_type, count in self.timer_counts.items():
                self.devices['voco'].properties[ str(timer_type) ].set_cached_value_and_notify( int(count) ) # Update the counts on the thing
        except Exception as ex:
            print("Error, could not update timer counts on the voco device: " + str(ex))



    # Turn Snips services on or off
    def set_snips_state(self, active=False):
        if self.persistent_data['listening'] != active:
            if self.DEBUG:
                print("Changing listening state to: " + str(active))
            self.persistent_data['listening'] = active
            self.save_persistent_data()
            if self.devices['voco'] != None:
                self.devices['voco'].properties['listening'].update( bool(active) )
        
        if self.token != None:
            try:
                if active == True:
                    self.set_status_on_thing("Listening")
                else:
                    self.set_status_on_thing("Not listening")
            except Exception as ex:
                print("Error setting listening state: " + str(ex))
        else:
            self.set_status_on_thing("Missing token, check settings")
            
        return
        
        # This is no longer used. It used to try and disable the hotword service, but this wasn't robust.
        
        try:
            if active == True:
                print("Setting to on")
                if self.hotword_process != None:
                    poll = self.hotword_process.poll()
                    if poll == None:
                        print("Hotword process seemed to already be running")
                    else:
                        print("Starting hotword (again)")
                        # (Re)Start the hotword detection
                        try:
                            hotword_command = [self.hotword_path,"-u",self.work_path,"-a",self.assistant_path,"-c",self.toml_path]
                            if self.DEBUG:
                                print("hotword_command = " + str(hotword_command))
                            self.hotword_process = Popen(hotword_command, env=my_env)
                            
                            self.set_status_on_thing("Listening")
                        except:
                            self.set_status_on_thing("Error starting")
                else:
                    print("self.hotword_process was None?")
                    try:
                        self.devices['voco'].connected = True
                        self.devices['voco'].connected_notify(True)
                        self.set_status_on_thing("Listening")
                    except:
                        print("Could not set thing as connected")
            else:
                print("stopping. Terminating hotword process.")
                try:
                    self.hotword_process.kill()
                    self.set_status_on_thing("Stopped")
                except:
                    print("Error while terminating hotword process")
                    self.set_status_on_thing("Error stopping")
               
        except Exception as ex:
            print("Error settings Snips state: " + str(ex))    



    def set_feedback_sounds(self,state):
        if self.DEBUG:
            print("User wants to switch feedback sounds to: " + str(state))
        try:
            self.devices['voco'].properties['feedback-sounds'].update( bool(state) )
            if bool(self.persistent_data['feedback_sounds']) != bool(state):
                self.persistent_data['feedback_sounds'] = bool(state)
                self.save_persistent_data()
        except Exception as ex:
            print("Error settings Snips feedback sounds preference: " + str(ex))

 
 
 
    def remove_thing(self, device_id):
        try:
            obj = self.get_device(device_id)        
            self.handle_device_removed(obj)                     # Remove voco thing from device dictionary
            if self.DEBUG:
                print("User removed Voco device")
        except:
            print("Could not remove things from devices")






#
#  PAIRING
#

    def start_pairing(self, timeout):
        """
        Start the pairing process. This starts when the user presses the + button on the things page.
        
        timeout -- Timeout in seconds at which to quit pairing
        """
        if self.pairing:
            #print("-Already pairing")
            return
          
        self.pairing = True
        return
    
    
    
    def cancel_pairing(self):
        """Cancel the pairing process."""
        self.pairing = False
        if self.DEBUG:
            print("End of pairing process. Checking if a new injection is required.")
            
        # Get all the things via the API.
        try:
            self.things = self.api_get("/things")
            print("Did the API call")
        except Exception as ex:
            print("Error, couldn't load things at init: " + str(ex))
        
            
        # Teach Snips the names of all the things
        #try:
        #    self.inject_updated_things_into_snips() # will check if there are new things/properties that Snips should learn about
        #except Exception as ex:
        #    print("Error, couldn't teach Snips the names of your things: " + str(ex))  




#
#  UNLOAD
#

    def unload(self):
        print("Shutting down Voco. Talk to you soon!")
               

        try:
            self.hotword_process.terminate()
            self.hotword_process.wait()
            print("Terminated the hotword")
        except Exception as ex:
            print("Error terminating the hotword process: " + str(ex))

        try:
            for process in self.external_processes:
                process.terminate()
                process.wait()
                print("Terminated Snips process")
        except Exception as ex:
            print("Error terminating the hotword process: " + str(ex))


        try:
            #
            self.mqtt_client.disconnect() # disconnect
            self.mqtt_client.loop_stop()    #Stop loop 
            #self.mqtt_client.loop.stop()
        except Exception as ex:
            print("Error cleanly closing Paho MQTT client: " + str(ex))
        
        
        #try:
            #self.mosquitto_process.terminate()
            #self.mosquitto_process.wait()
            #print("Terminated mosquitto")
        #except Exception as ex:
        #    print("Error terminating the mosquitto process: " + str(ex))
        
        self.running = False




#
#  API
#

    def api_get(self, api_path):
        """Returns data from the WebThings Gateway API."""
        if self.DEBUG:
            print("GET PATH = " + str(api_path))
        #print("GET TOKEN = " + str(self.token))
        if self.token == None:
            print("PLEASE ENTER YOUR AUTHORIZATION CODE IN THE SETTINGS PAGE")
            self.set_status_on_thing("Authorization code missing, check settings")
            return []
        
        try:
            r = requests.get(self.server + api_path, headers={
                  'Content-Type': 'application/json',
                  'Accept': 'application/json',
                  'Authorization': 'Bearer ' + str(self.token),
                }, verify=False, timeout=3)
            if self.DEBUG:
                print("API GET: " + str(r.status_code) + ", " + str(r.reason))

            if r.status_code != 200:
                if self.DEBUG:
                    print("API returned a status code that was not 200. It was: " + str(r.status_code))
                return {"error": str(r.status_code)}
                
            else:
                return json.loads(r.text)
            
        except Exception as ex:
            print("Error doing http request/loading returned json: " + str(ex))
            self.speak("I could not connect. ")
            #return [] # or should this be {} ? Depends on the call perhaps.
            return {"error": 500}


    def api_put(self, api_path, json_dict):
        """Sends data to the WebThings Gateway API."""

        if self.DEBUG:
            print("PUT > api_path = " + str(api_path))
            print("PUT > json dict = " + str(json_dict))
            print("PUT > self.server = " + str(self.server))


        headers = {
            'Accept': 'application/json',
            'Authorization': 'Bearer {}'.format(self.token),
        }
        try:
            r = requests.put(
                self.server + api_path,
                json=json_dict,
                headers=headers,
                verify=False,
                timeout=5
            )
            if self.DEBUG:
                print("API PUT: " + str(r.status_code) + ", " + str(r.reason))

            if r.status_code != 200:
                print("Error communicating: " + str(r.status_code))
                return {"error": str(r.status_code)}
            else:
                return json.loads(r.text)

        except Exception as ex:
            print("Error doing http request/loading returned json: " + str(ex))
            self.speak("I could not connect. ")
            #return {"error": "I could not connect to the web things gateway"}
            #return [] # or should this be {} ? Depends on the call perhaps.
            return {"error": 500}



#
#  PERSISTENCE
#

    def save_persistent_data(self):
        if self.DEBUG:
            print("Saving to persistence data store at path: " + str(self.persistence_file_path))
            
        try:
            if not os.path.isfile(self.persistence_file_path):
                open(self.persistence_file_path, 'a').close()
                if self.DEBUG:
                    print("Created an empty persistence file")
            else:
                if self.DEBUG:
                    print("Persistence file existed. Will try to save to it.")

            with open(self.persistence_file_path) as f:
                if self.DEBUG:
                    print("saving persistent data: " + str(self.persistent_data))
                json.dump( self.persistent_data, open( self.persistence_file_path, 'w+' ) )
                return True

        except Exception as ex:
            print("Error: could not store data in persistent store: " + str(ex) )
            return False







#
# MQTT
#


    # In the end Hermes proved unreliable and not flexible enough.
    def start_mqtt_client(self):
        try:
            self.mqtt_client = client.Client(client_id="extra_snips_detector")
            HOST = "localhost"
            PORT = 1883
            self.mqtt_client.on_connect = self.on_connect
            self.mqtt_client.on_message = self.on_message
            self.mqtt_client.connect(HOST, PORT, keepalive=60)
            self.mqtt_client.loop_forever()
        except Exception as ex:
            print("Error creating extra MQTT connection: " + str(ex))



    # Subscribe to the important messages
    def on_connect(self, client, userdata, flags, rc):
        self.mqtt_client.subscribe("hermes/hotword/#")
        self.mqtt_client.subscribe("hermes/intent/#")



    # Process a message as it arrives
    def on_message(self, client, userdata, msg):
        
        # If listening is set to false, ignore everything. Originally the hotword detector was turned off, but that caused reliability issues.
        if self.persistent_data['listening'] == False:
            return
        
        try:
            if self.DEBUG:
                print("MQTT msg.topic = " + str(msg.topic))

            if msg.topic.startswith('hermes/hotword'):
                if msg.topic.endswith('/detected'):
                    self.intent_received = False
                    if self.DEBUG:
                        print(">> Hotword detected")
                    if self.persistent_data['feedback_sounds'] == True:
                        self.play_sound( str(self.start_of_input_sound) )

                #elif msg.topic.endswith('/toggleOff'):
                #    self.play_sound(str(self.alarm_sound) )
                
                elif msg.topic.endswith('/toggleOn'):
                    if self.persistent_data['feedback_sounds'] == True and self.intent_received == False:
                        if self.DEBUG:
                            print("No intent received")
                        self.play_sound(str(self.end_of_input_sound) )
                        self.intent_received = True
                
                # TODO: To support satelites it will be necessary to 'throw the voice' via the Snips audio server:
                #binaryFile = open(self.listening_sound, mode='rb')
                #wav = bytearray(binaryFile.read())
                #publish.single("hermes/audioServer/{}/playBytes/whateverId".format("default"), payload=wav, hostname="localhost", client_id="") 
                
            else:
                self.intent_received = True
                if self.DEBUG:
                    print("-----------------------------------")
                    print(">> Received intent message.")
                    print("message received "  + str(msg.payload.decode("utf-8")))
                    print("message topic = " + str(msg.topic))
                intent_name = os.path.basename(os.path.normpath(msg.topic))
                
                intent_message = json.loads(msg.payload.decode("utf-8"), object_hook=lambda d: Namespace(**d)) # Allows for the use of dot notation.3

                # End the existing session
                self.mqtt_client.publish('hermes/dialogueManager/endSession', json.dumps({"text": "", "sessionId": intent_message.sessionId}))
                
                # Deal with the user's command
                self.master_intent_callback(intent_message)
                
        except Exception as ex:
            print("Error in Paho receive: " + str(ex))







    
#
# ROUTING
#

    def master_intent_callback(self, intent_message):    # Triggered everytime Snips succesfully recognizes a voice intent
        self.intent_received = True
        
        try:
            incoming_intent = str(intent_message.intent.intentName)
            sentence = str(intent_message.input).lower()


            if self.DEBUG:
                print("")
                print("")
                print(">>")
                print(">> incoming intent   : " + incoming_intent)
                print(">> intent_message    : " + sentence)
                print(">> session ID        : " + str(intent_message.sessionId))
                print(">>")
                  

            # check if there are multiple words in the sentence
            word_count = 1
            for i in sentence: 
                if i == ' ': 
                    word_count += 1
                    
            if word_count < 2:
                if sentence == 'hello' or sentence == 'allow':
                    self.speak("Hello")
                else:    
                     self.speak("I didn't get that") 

                return
        
            if 'unknownword' in sentence:
                self.speak("I didn't quite get that")
                return


        except Exception as ex:
            print("Error at beginning of master intent callback: " + str(ex))
        
        try:
            slots = self.extract_slots(intent_message)
            if self.DEBUG:
                print("INCOMING SLOTS = " + str(slots))
        except Exception as ex:
            print("Error extracting slots at beginning of intent callback: " + str(ex))
        
        # Get all the things data via the API
        try:
            self.things = self.api_get("/things")
        except Exception as ex:
            print("Error, couldn't load things: " + str(ex))

        try:
            # Alternative routing. Some heuristics, since Snips sometimes chooses the wrong intent.
            
            
            # Alternative route to get_boolean.
            try:
                if incoming_intent == 'createcandle:get_value' and str(slots['property']) == "state":          
                    if self.DEBUG:
                        print("using alternative route to get_boolean")
                    incoming_intent = 'createcandle:get_boolean'
            except:
                print("alternate route 1 failed")
            
            try:
                if incoming_intent == 'createcandle:set_state' and str(slots['boolean']) == "state":          
                    if self.DEBUG:
                        print("using alternative route to get_boolean")
                    incoming_intent = 'createcandle:get_boolean'
            except:
                print("alternate route 2 failed")
            
            
            # Alternative route to set state
            try:
                if incoming_intent == 'createcandle:set_timer' and sentence.startswith("turn"):          
                    
                    if sentence.startswith("turn on"):
                        incoming_intent = 'createcandle:set_state'
                        slots['boolean'] = True
                        if self.DEBUG:
                            print("using alternative route to set state")
                    elif sentence.startswith("turn off"):
                        incoming_intent = 'createcandle:set_state'
                        slots['boolean'] = False
                        if self.DEBUG:
                            print("using alternative route to set state")
                    
                    
                    
            except:
                print("alternate route 1 failed")
            
                
            # Avoid setting a value if no value is present
            try:
                if incoming_intent == 'createcandle:set_value' and slots['color'] is None and slots['number'] is None and slots['percentage'] is None and slots['string'] is None:
                    if slots['boolean'] != None:
                        #print("Routing set_value to set_state instead")
                        incoming_intent == 'createcandle:set_state' # Switch to another intent type which has a better shot.
                    else:
                        if self.DEBUG:
                            print("request did not contain a valid value to set to")
                        self.speak("Your request did not contain a valid value.")
                        #hermes.publish_end_session_notification(intent_message.site_id, "Your request did not contain a valid value.", "")
                        return
            except:
                print("alternate route 3 failed")
            

            # Normal routing
            if incoming_intent == 'createcandle:get_time':
                intent_get_time(self, slots, intent_message)
            elif incoming_intent == 'createcandle:set_timer':
                intent_set_timer(self, slots, intent_message)
            elif incoming_intent == 'createcandle:get_timer_count':
                intent_get_timer_count(self, slots, intent_message)
            elif incoming_intent == 'createcandle:list_timers':
                intent_list_timers(self, slots, intent_message)
            elif incoming_intent == 'createcandle:stop_timer':
                intent_stop_timer(self, slots, intent_message)
            elif incoming_intent == 'createcandle:get_value':
                intent_get_value(self, slots, intent_message)
            elif incoming_intent == 'createcandle:set_state':
                intent_set_state(self, slots, intent_message)
            elif incoming_intent == 'createcandle:set_value':
                intent_set_value(self, slots, intent_message, None)
            elif incoming_intent == 'createcandle:get_boolean':
                intent_get_boolean(self, slots, intent_message)

            else:
                if self.DEBUG:
                    print("Error: the code could not handle that intent. Under construction?")
                self.speak("Sorry, I did not understand your intention.")
                
        except Exception as ex:
            print("Error during routing: " + str(ex))



    # Update Snips with the latest names of things and properties. This helps to improve recognition.
    def inject_updated_things_into_snips(self, force_injection=False):
        """ Teaches Snips what the user's devices and properties are called """
        #if self.DEBUG:
        #    print("Checking if new things/properties/strings should be injected into Snips")
        try:
            # Check if any new things have been created by the user.
            #if datetime.utcnow().timestamp() - self.last_injection_time < self.minimum_injection_interval:
            #    if self.DEBUG:
            #        print("Not enough time has passed - will not try to inject the new thing/property/string names.")
            #        print(str(datetime.utcnow().timestamp() - self.last_injection_time) + " versus " + str(self.minimum_injection_interval))
            #    return
                
            #else: 
            #if True: # just a quick hack
                #self.attempting_injection = True
                #self.last_injection_time = datetime.utcnow().timestamp()
            #if self.DEBUG:
            #    print("Checking if Snips should be updated with new thing/property/string names")
            
            fresh_thing_titles = set()
            fresh_property_titles = set()
            fresh_property_strings = set()

            for thing in self.things:
                if 'title' in thing:
                    fresh_thing_titles.add(clean_up_string_for_speaking(str(thing['title']).lower()))
                    for thing_property_key in thing['properties']:
                        if 'type' in thing['properties'][thing_property_key] and 'enum' in thing['properties'][thing_property_key]:
                            if thing['properties'][thing_property_key]['type'] == 'string':
                                for word in thing['properties'][thing_property_key]['enum']:
                                    fresh_property_strings.add(str(word))
                        if 'title' in thing['properties'][thing_property_key]:
                            fresh_property_titles.add(clean_up_string_for_speaking(str(thing['properties'][thing_property_key]['title']).lower()))
            
            operations = []
            
            #if self.DEBUG:
                #print("fresh_thing_titles = " + str(fresh_thing_titles))
                #print("fresh_prop_titles = " + str(fresh_property_titles))
                #print("fresh_prop_strings = " + str(fresh_property_strings))
            
            try:
                thing_titles = set(self.persistent_data['thing_titles'])
                property_titles = set(self.persistent_data['property_titles'])
                property_strings = set(self.persistent_data['property_strings'])
            except:
                print("Couldn't load previous thing data from persistence. If Snips was just installed this is normal.")
                thing_titles = set()
                property_titles = set()
                property_strings = set()

            if len(thing_titles^fresh_thing_titles) > 0 or force_injection == True:                           # comparing sets to detect changes in thing titles
                if self.DEBUG:
                    print("Teaching Snips the updated thing titles:")
                    print(str(list(fresh_thing_titles)))
                #operations.append(
                #    AddFromVanillaInjectionRequest({"Thing" : list(fresh_thing_titles) })
                #)
                operation = ('addFromVanilla',{"Thing" : list(fresh_thing_titles) })
                operations.append(operation)
                
            if len(property_titles^fresh_property_titles) > 0 or force_injection == True:
                if self.DEBUG:
                    print("Teaching Snips the updated property titles:")
                    print(str(list(fresh_property_titles)))
                #operations.append(
                #    AddFromVanillaInjectionRequest({"Property" : list(fresh_property_titles) + self.extra_properties + self.capabilities + self.generic_properties + self.numeric_property_names})
                #)
                operation = ('addFromVanilla',{"Property" : list(fresh_property_titles) })
                operations.append(operation)

            if len(property_strings^fresh_property_strings) > 0 or force_injection == True:
                if self.DEBUG:
                    print("Teaching Snips the updated property strings:")
                    print(str(list(fresh_property_strings)))
                #operations.append(
                #    AddFromVanillaInjectionRequest({"string" : list(fresh_property_strings) })
                #)
                operation = ('addFromVanilla',{"string" : list(fresh_property_strings) })
                operations.append(operation)
                
            #if self.DEBUG:
            #    print("operations: " + str(operations))
                    
                    
            # Check if Snips should be updated with fresh data
            if operations != []:
                update_request = {"operations":operations}
            
                if self.DEBUG:
                    print("Updating Snips! update_request json: " + str(json.dumps(update_request)))
                
                try:
                    self.persistent_data['thing_titles'] = list(fresh_thing_titles)
                    self.persistent_data['property_titles'] = list(fresh_property_titles)
                    self.persistent_data['property_strings'] = list(fresh_property_strings)
                    self.save_persistent_data()
                except Exception as ex:
                     print("Error saving thing details to persistence: " + str(ex))
                
                try:
                    
                    if self.mqtt_client != None:
                        print("Injection: self.mqtt_client exists, will try to inject")
                        print(str(json.dumps(operations)))
                        self.mqtt_client.publish('hermes/injection/perform', json.dumps(update_request))
                    
                        print("Injection published to MQTT")
                    #with Hermes("localhost:1883") as herm:
                    #    herm.request_injection(update_request)
                    
                    self.last_injection_time = time.time() #datetime.utcnow().timestamp()
                
                except Exception as ex:
                     print("Error during injection: " + str(ex))

            self.attempting_injection = False

        except Exception as ex:
            print("Error during analysis and injection of your things into Snips: " + str(ex))



#
# THING SCANNER
#

    # This function looks up things that might be a match to the things names or property names that the user mentioned in their request.
    def check_things(self, actuator, target_thing_title, target_property_title, target_space ):
        if self.DEBUG:
            print("SCANNING THINGS")

        if target_thing_title == None and target_property_title == None and target_space == None:
            print("No useful input available for a search through the things. Cancelling...")
            return []
        
        
        result = [] # This will hold all found matches

        if target_thing_title is None:
            if self.DEBUG:
                print("No thing title supplied. Will try to matching properties in all devices.")
        else:
            target_thing_title = str(target_thing_title).lower()
            if self.DEBUG:
                print("-> target thing title is: " + str(target_thing_title))
        
        
        if target_property_title is None:
            if self.DEBUG:
                print("-> No property title provided. Will try to get relevant properties.")
        else:
            target_property_title = str(target_property_title).lower()
            if self.DEBUG:
                print("-> target property title is: " + str(target_property_title))
        
        if target_space != None:
            if self.DEBUG:
                print("-> target space is: " + str(target_property_title))
        
        
        try:
            if self.things == None or self.things == []:
                print("Error, the things dictionary was empty. Please provice an API key in the add-on setting (or add some things).")
                return
            
            for thing in self.things:
                

                # TITLE
                
                try:
                    current_thing_title = str(thing['title']).lower()
                    probable_thing_title = None    # Used later, by the back-up way of finding the correct thing.
                except:
                    if self.DEBUG:
                        print("Notice: thing had no title")
                    try:
                        current_thing_title = str(thing['name']).lower()
                    except:
                        if self.DEBUG:
                            print("Warning: thing had no name either. Skipping it.")
                        continue

                try:
                    #if self.DEBUG:
                        #print("")
                        #print("___" + current_thing_title)
                    probable_thing_title_confidence = 100
                    
                    if target_thing_title == None:  # If no thing title provided, we go over every thing and let the property be leading in finding a match.
                        pass
                    
                    elif target_thing_title == current_thing_title:   # If the thing title is a perfect match
                        probable_thing_title = current_thing_title
                        if self.DEBUG:
                            print("FOUND THE CORRECT THING: " + str(current_thing_title))
                    elif fuzz.ratio(str(target_thing_title), current_thing_title) > 85:  # If the title is a fuzzy match
                        if self.DEBUG:
                            print("This thing title is pretty similar, so it could be what we're looking for: " + str(current_thing_title))
                        probable_thing_title = current_thing_title
                        probable_thing_title_confidence = 85
                    elif target_space != None:
                        space_title = str(target_space) + " " + str(target_thing_title)
                        #if self.DEBUG:
                        #   print("space title = " + str(target_space) + " + " + str(target_thing_title))
                        if fuzz.ratio(space_title, current_thing_title) > 85:
                            probable_thing_title = space_title
                        
                    elif current_thing_title.startswith(target_thing_title):
                        if self.DEBUG:
                            print("partial match:" + str(len(current_thing_title) / len(target_thing_title)))
                        if len(current_thing_title) / len(target_thing_title) < 2:
                            # The strings mostly start the same, so this might be a match.
                            probable_thing_title = current_thing_title
                            probable_thing_title_confidence = 25
                    else:
                        # A title was provided, but we were not able to match it to the current things. Perhaps we can get a property-based match.
                        continue
                        
                except Exception as ex:
                    print("Error while trying to match title: " + str(ex))



                # PROPERTIES
                
                try:
                    for thing_property_key in thing['properties']:
                        #print("Property details: " + str(thing['properties'][thing_property_key]))

                        try:
                            current_property_title = str(thing['properties'][thing_property_key]['title']).lower()
                        except:
                            if self.DEBUG:
                                print("could not extract title from WebThings property data. try Name instead.")
                            try:
                                current_property_title = str(thing['properties'][thing_property_key]['name']).lower()
                            except:
                                current_property_title = str(thing_property_key)
                                if self.DEBUG:
                                    print("Couldn't find a property name either. Title has now been set to key: " + str(current_property_title))

                        
                        # Get basic info

                        # This dictionary holds properties of the potential match. There can be multiple matches, for example if the user wants to hear the temperature level of all things.
                        match_dict = {
                                "thing": probable_thing_title,
                                "property": current_property_title,
                                "confidence": probable_thing_title_confidence,
                                "type": None,
                                "readOnly": None,
                                "@type": None,
                                "property_url": get_api_url(thing['properties'][thing_property_key]['links'])
                                }

                        try:
                            if 'type' in thing['properties'][thing_property_key]:
                                match_dict['type'] = thing['properties'][thing_property_key]['type']
                            else:
                                match_dict['type'] = None # This is a little too precautious, since the type should theoretically always be defined?
                        except Exception as ex:
                            if self.DEBUG:
                                print("Error while checking property type: "  + str(ex))
                            match_dict['type'] = None

                            
                        try:
                            # Check if it's a read-only property
                            if 'readOnly' in thing['properties'][thing_property_key]:
                                match_dict['readOnly'] = bool(thing['properties'][thing_property_key]['readOnly'])
                            #else: 
                            #    match_dict['readOnly'] = None
                        except Exception as ex:
                            if self.DEBUG:
                                print("Error looking up readOnly value: "  + str(ex))
                            #match_dict['readOnly'] = None # TODO in theory this should not be necessary. In practise, weirdly, it is.

                        try:
                            if '@type' in thing['properties'][thing_property_key]:
                                match_dict['@type'] = thing['properties'][thing_property_key]['@type'] # Looking for things like "OnOffProperty"
                            #else:
                            #    match_dict['@type'] = None
                        except Exception as ex:
                            print("Error looking up capability @type: "  + str(ex))
                            pass
                        
                        # TODO: add proper ordinal support via the built-in Snips slot
                        numerical_index = None
                        try:
                            numerical_index = self.numeric_property_names.index(target_property_title)
                        except:
                            #print("name was not in numerical index list (so not 'third' or 'second')")
                            pass
                        
                        # Avoid properties that the add-on can't deal with.
                        if match_dict['@type'] == "VideoProperty" or match_dict['@type'] == "ImageProperty":
                            continue


                        # Start looking for a matching property
                        
                        # No target property title set
                        if match_dict['thing'] != None and (target_property_title in self.generic_properties or target_property_title == None):
                            
                            if self.DEBUG:
                                print("Property title was not or abstractly supplied, so adding " + str(match_dict['property']) + " to the list")
                            result.append(match_dict.copy())
                            continue
                        
                        # If we found the thing and it only has one property, then use that.
                        elif match_dict['thing'] != None and target_property_title != None and len(thing['properties']) == 1:
                            result.append(match_dict.copy())
                            continue
                        
                        # Looking for a state inside a matched thing.
                        elif target_property_title == 'state' and match_dict['thing'] != None:
                            if self.DEBUG:
                                print("looking for a 'state' (a.k.a. boolean type)")
                            #print("type:" + str(thing['properties'][thing_property_key]['type']))
                            if thing['properties'][thing_property_key]['type'] == 'boolean':
                                # While looking for state, found a boolean
                                result.append(match_dict.copy())
                                continue
                        
                        # Looking for a level inside a matched thing.
                        elif target_property_title == 'level' and match_dict['thing'] != None:
                            if self.DEBUG:
                                print("looking for a 'level'")
                            #print("type:" + str(thing['properties'][thing_property_key]['type']))
                            if thing['properties'][thing_property_key]['type'] != 'boolean':
                                # While looking for level, found a non-boolean
                                result.append(match_dict.copy())
                                continue

                        # Looking for a value
                        elif target_property_title == 'value' and match_dict['thing'] != None:
                            result.append(match_dict.copy())
                            continue
                            
                        # Looking for 'all' properties
                        elif target_property_title == 'all' and match_dict['thing'] != None:
                            #If all properties are desired, add all properties
                            result.append(match_dict.copy())
                            continue
                        
                        # We found a good matching property title and already found a good matching thing title. # TODO: shouldn't this be higher up?
                        elif fuzz.ratio(current_property_title, target_property_title) > 85:
                            if self.DEBUG:
                                print("FOUND A PROPERTY WITH THE MATCHING FUZZY NAME")
                            if match_dict['thing'] == None:
                                match_dict['thing'] = current_thing_title
                                result.append(match_dict.copy())
                            else:
                                result = [] # Since this is a really good match, we remove any older properties we may have found.
                                result.append(match_dict.copy())
                                return result

                            
                        # We're looking for a numbered property (e.g. moisture 5), and this property has that number in it. Here we favour sensors. # TODO: add ordinal support?
                        elif str(numerical_index) in current_property_title and target_thing_title != None:
                            result.append(match_dict.copy())
                            
                            if thing['properties'][thing_property_key]['type'] == 'boolean' and probability_of_correct_property == 0:
                                probability_of_correct_property = 1
                                match_dict['property'] = current_property_title
                                #match_dict['type'] = thing['properties'][thing_property_key]['type']
                                match_dict['property_url'] = get_api_url(thing['properties'][thing_property_key]['links'])

                            if thing['properties'][thing_property_key]['type'] != 'boolean' and probability_of_correct_property < 2:
                                probability_of_correct_property = 1
                                match_dict['property'] = current_property_title
                                #match_dict['type'] = thing['properties'][thing_property_key]['type']
                                match_dict['property_url'] = get_api_url(thing['properties'][thing_property_key]['links'])
                                #if match_dict['property_url'] != None: # If we found anything, then append it.
                                #    result.append(match_dict.copy())

                
                
                except Exception as ex:
                    if self.DEBUG:
                        print("Error while looping over property: " + str(ex))

                # If the thing title matches and we found at least one property, then we're done.
                if probable_thing_title != None and len(result) == 1:
                    return result
                
                # If there are multiple results, we finally take the initial preference of the intent into account and prune properties accordingly.
                elif len(result) > 1:
                    for found_property in result:
                        if found_property['type'] == 'boolean' and actuator == False: # Remote property if it's not the type we're looking for
                            #print("pruning boolean property")
                            del found_property
                        elif found_property['type'] != 'boolean' and actuator == True: # Temove property if it's not the type we're looking for
                            #print("pruning non-boolean property")
                            del found_property

                # TODO: better handling of what happens if the thing title was not found. The response could be less vague than 'no match'.
                
        except Exception as ex:
            print("Error while looking for match in things: " + str(ex))
            
        return result




    # This function parses the data coming from Snips and turned it into an easy to use dictionary.
    def extract_slots(self,intent_message):
        """Parses incoming data from Snips into an easy to use dictionary"""

        # TODO: better handle 'now' as a start time. E.g. Turn on the lamp from now until 5 o'clock. Although it does already work ok.

        slots = {"sentence":None,       # The full original sentence
                "thing":None,           # Thing title
                "property":None,        # Property title
                "space":None,           # Room name
                "boolean":None,         # On or Off, Open or Closed, Locked or Unlocked
                "number":None,          # A number
                "percentage":None,      # A percentage
                "string":None,          # E.g. to set the value of a dropdown. For now should only be populated by an injection at runtime, based on the existing dropdown values.
                "time_string":None,     # the snippet of the sentence describing the time the user spoke.
                "color":None,           # E.g. 'green'. Similar to the string.
                "start_time":None,      # If this exists, there is also an end-time.
                "end_time":None,        # An absolute time
                "special_time":None,    # relative times like "sunrise"
                "duration":None,        # E.g. 5 minutes
                "period":None,          # Can only be 'in' or 'for'. Used to distinguish "turn on IN 5 minutes" or "turn on FOR 5 minutes"
                "timer_type":None,      # Can be timer, alarm, reminder, countdown
                "timer_last":None       # Used to deterine how many timers a user wants to manipulate. Can only be "all" or "last". E.g. "The last 5 timers"
                }

        #print("incoming slots: " + str(vars(intent_message.slots)))

        try:
            sentence = str(intent_message.input).lower()
            try:
                sentence = sentence.replace("unknownword","") # TODO: perhaps notify the user that the sentence wasn't fully understood. Perhaps make it an option: try to continue, or ask to repeat the command.
            except:
                pass
            slots['sentence'] = sentence
        except Exception as ex:
            print("Could not extract full sentence into a slot: " + str(ex))

        for item in intent_message.slots:
            try:                
                if item.value.kind == 'InstantTime':
                    if self.DEBUG:
                        print("handling instantTime")
                        
                    try:
                        #d = datetime.utcnow()
                        #epoch = datetime(1970,1,1)
                        #t = (d - epoch).total_seconds()
                        #print("t = " + str(int(t)))
                    
                        #print("self.current_utc_time: " + str(self.current_utc_time))
                    
                        #print("datetime.now() = " + str(datetime.now()))
                        slots['time_string'] = item.rawValue # The time as it was spoken
                        #print("InstantTime slots['time_string'] = " + slots['time_string'])
                        #print("instant time object: " + str(item.value.value))
                        ignore_timezone = True
                        if slots['time_string'].startswith("in"):
                            ignore_timezone = False
                        utc_timestamp = self.string_to_utc_timestamp(item.value.value,ignore_timezone)
                        #print("current time as stamp: " + str(self.current_utc_time))
                        #print("target time: " + str(utc_timestamp))
                        if utc_timestamp > self.current_utc_time: # Only allow moments in the future
                            slots['end_time'] = utc_timestamp
                        else:
                            slots['end_time'] = utc_timestamp + 43200 # add 12 hours
                            #self.speak("The time you stated seems to be in the past.") # If after all that the moment is still in the past
                            #return []
                    except Exception as ex:
                        print("instantTime extraction error: " + str(ex))
                    
                elif item.value.kind == 'TimeInterval':
                    try:
                        slots['time_string'] = item.rawValue # The time as it was spoken
                        print("TimeInterval slots['time_string'] = " + slots['time_string'])
                        print("a")
                        try:
                            utc_timestamp = self.string_to_utc_timestamp(item.value.to)
                            if utc_timestamp > self.current_utc_time: # Only allow moments in the future
                                slots['end_time'] = utc_timestamp
                            else:
                                slots['end_time'] = utc_timestamp + 43200 # add 12 hours
                            print("b")
                        except Exception as ex:
                            print("timeInterval end time extraction error" + str(ex))
                        try:
                            utc_timestamp = self.string_to_utc_timestamp(item.value['from'])
                            print("c")
                            if utc_timestamp > self.current_utc_time: # Only allow moments in the future
                                slots['start_time'] = utc_timestamp        
                            else:
                                slots['start_time'] = utc_timestamp + 43200 # add 12 hours
                                #self.speak("The time you stated seems to be in the past.") # If after all that the moment is still in the past
                                #return []
                        except Exception as ex:
                            print("timeInterval start time extraction error" + str(ex))
                    except Exception as ex:
                        print("timeInterval extraction error: " + str(ex))

                elif item.value.kind == 'Duration':
                    slots['time_string'] = item.rawValue # The time as it was spoken
                    target_time_delta = item.value.seconds + item.value.minutes * 60 + item.value.hours * 3600 + item.value.days * 86400 + item.value.weeks * 604800 # TODO: Could also support years, in theory..
                    # Turns the duration into the absolute time when the duration ends
                    if target_time_delta != 0:
                        slots['duration'] = self.current_utc_time + int(target_time_delta)

                elif item.slotName == 'special_time':
                    pass
                    # TODO here we could handle things like 'at dawn', 'at sundown' and 'at sunrise', as long as those could be calculated without looking it up online somehow.
                
                elif item.slotName == 'pleasantries':
                    if item.value.value.lower() == "please":
                        self.pleasantry_count += 1 # TODO: We count how often the user has said 'please', so that once in a while Snips can be thankful for the good manners.
                    else:
                        slots['pleasantries'] = item.value.value # For example, it the sentence started with "Can you" it could be nice to respond with "I can" or "I cannot".

                else:
                    if slots[item.slotName] == None:
                        slots[item.slotName] = item.value.value
                    else:
                        slots[item.slotName] = slots[item.slotName] + " " + item.value.value # TODO: in the future multiple thing titles should be handled separately. All slots should probably be lists.

            except Exception as ex:
                print("Error getting while looping over incoming slots data: " + str(ex))   

        return slots



    #def local_time_string_to_epoch_stamp(self,date_string):
    #    aware_datetime = parse(date_string)
    #    naive_utc_datetime = aware_datetime.astimezone(timezone('utc')).replace(tzinfo=None)
    #    epoch_stamp = naive_utc_datetime.timestamp()



    def string_to_utc_timestamp(self,date_string,ignore_timezone=True):
        """ date as a date object """
        
        try:
            if date_string == None:
                print("string_to_utc_timestamp: date string was None.")
                return 0
                
            if self.DEBUG:
                print("string_to_utc_timestamp. Date string: " + str(date_string))
            
            if(ignore_timezone):
                print("ignoring timezone")
                if '+' in date_string:
                    simpler_times = date_string.split('+', 1)[0]
                else:
                    simpler_times = date_string
                print("@split string: " + str(simpler_times))
                naive_datetime = parse(simpler_times)
                print("@naive datetime: " + str(naive_datetime))
                localized_datetime = self.user_timezone.localize(naive_datetime)
                if self.DEBUG:
                    print("@localized_datetime: " + str(localized_datetime))
                localized_timestamp = int(localized_datetime.timestamp()) #- self.seconds_offset_from_utc
            else:
                print("accounting for timezone")
                aware_datetime = parse(date_string)
                print("aware datetime = " + str(aware_datetime))
                naive_datetime = aware_datetime.astimezone(timezone(self.time_zone)).replace(tzinfo=None)
                print("naive date object: " + str(naive_datetime))
                localized_datetime = self.user_timezone.localize(naive_datetime)
                localized_timestamp = localized_datetime.timestamp()
                print("localized_timestamp = " + str(localized_timestamp))
                print("time.time = " + str(time.time()))
                if self.DEBUG:
                    print("@localized_timestamp = " + str(localized_timestamp))
                
            #print("self.seconds_offset_from_utc (not used) = " + str(self.seconds_offset_from_utc))
            return int(localized_timestamp)
        except Exception as ex:
            print("Error in string to UTC timestamp: " + str(ex))
            return 0


    def human_readable_time(self,utc_timestamp):
        """ moment is as UTC timestamp, timezone_offset is in seconds """
        try:
            localized_timestamp = int(utc_timestamp) + self.seconds_offset_from_utc
            hacky_datetime = datetime.utcfromtimestamp(localized_timestamp)

            if self.DEBUG:
                print("human readable hour = " + str(hacky_datetime.hour))
                print("human readable minute = " + str(hacky_datetime.minute))
            
            hours = hacky_datetime.hour
            minutes = hacky_datetime.minute
            combo_word = " past "
            end_word = ""
            
            # Minutes
            if minutes == 45:
                hours += 1
                combo_word = " to "
                minutes = "a quarter"
            elif minutes > 45:
                hours += 1
                combo_word = " to "
                minutes = 60 - minutes # switches minutes to between 1 and 14, and increases the hour count
            elif minutes == 0 and hours != 24:
                combo_word = ""
                minutes = ""
                end_word = " o' clock"
            elif minutes == 30:
                minutes = "half"

            if type(minutes) == int:
                if minutes == 1:
                    minutes = "1 minute"
                else:
                    minutes = str(minutes) + " minutes"
            
            # Hours
            if hours != 12:
                hours = hours % 12
            if hours == 0:
                hours = "midnight"
                end_word = ""
            
            nice_time = str(minutes) + str(combo_word) + str(hours) + str(end_word)

            if self.DEBUG:
                print(str(nice_time))
                
            return nice_time
            
        except Exception as ex:
            print("Error making human readable time: " + str(ex))
            return ""






