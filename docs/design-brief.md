# Eufy Siren Application - Design Brief

## Context

By default, the Eufy security eco-system has very limited options to sound a siren when motion is detected. The [Eufy Siren](https://www.eufy.com/au/products/eufy-security-siren-105-db-wireless-alarm) is not nearly loud enough. The goal of this project is to control a simple Smart Switch (Shelly or Tasmota) that can be used to turn on a basic wired siren when motion is detected by a Eufy Camera and certain conditions are satisfied. 

The control flow is as follows:

1. The Eufy cameras are [integrated with the Apple Home ecosystem](https://service.eufy.com/article-description/HomeKit-on-eufySecurity-Devices)
2. In Appel Home, an integration is setup for each Eufy camera exposed in Apple Home. This integration invokes a shortcut which does a `Get content of URL`  action. 
3. The URL is configured to use one of the predefined end points of this eufy-siren app, for example [http://192.168.86.99:8085/motion/camera1](http://192.168.86.99:8085/motion/camera1) 
4. When motion is detected by the Eufy camera, the relevant URL in this eufy-siren app will be called. 
5. When the conditions are right (for example, two motion events received from 2 different cameras at least 20 seconds apart), the app will turn on the smart switch.
6. The smart switch powers a 12V PSU which in turn sends 12V power to the siren which sounds.
7. The app will automatically turn off the smart switch and so the siren a set period of time after the motion events have stopped.   

## Technical Requirements

- The shell of the eufy-siren app has been created at ~/dev/eufy-siren.
- Use the sc-smart-device library to control the smart switch. This library has already been added to the project and is documented here: [https://spello-consulting.github.io/sc-smart-device/](https://spello-consulting.github.io/sc-smart-device/)
- This will be a multi-threaded application:
    1. Master controller thread
    2. SmartDeviceWorker thread to control the smart switch
    3. API thread responding to GET requests posted by Apple Home. 
- Use the [ThreadManager class](https://spello-consulting.github.io/sc-foundation/reference/thread_manager/) from the sc-foundation-services library to manage and orchestrate threads.
- For an example project that also uses the sc-smart-device library and the ThreadManager class, see ~/dev/PowerController
- A YAMl configuration file has been created that should contain most of the settings required for this app: `configs/development.yaml` 
- Create a full pytest suite. Testing can set SCSmartDevices.Devices\[\].Simulate = True to simulate a smart switch.

## Functional Requirements

- The app will accept any configured API endpoint
- App API requests (valid or not) are logged
- Starting the siren means turning on the Smart Device switch as specified in the Siren.Switch configuration parameter. This must reference a valid smart switch output under SCSmartDevices.Devices\[\].Outputs\[\]
- The endpoint action of StartSiren will start the siren immediately regardless of whether any motion conditions have been met.
- The endpoint action of StopSiren will start the siren immediately and the post trigger sleep interval will begin.
- Nominally, the siren will be started when one or more motion events (Service API endpoints of action Motion) are received.
- If the configuration calls for more than one motion event (Siren.MinMotionEvents) before the siren is started, then all the events must be separated by at least Siren.MinMotionInterval seconds and no more than Siren.MaxMotionInterval seconds).
- If Siren.MinMotionSources is greater than one, this means that motion events must be received by more than one Motion end point within this Siren.MinMotionInterval /  Siren.MaxMotionInterval window. For example, if Siren.MinMotionSources = 2, then we must receive motion events from at least 2 unique endpoints.
- Create the [readme.md](https://readme.md "https://readme.md") file with context, installation and configuration instructions including guidelines for setting up: 
    - The Eufy Security > Apple Home integration
    - Setting up the appropriate Apple Home integration for each camera to drive the ServiceAPI calls.
- There is no requirement for a web UI for this project.
  