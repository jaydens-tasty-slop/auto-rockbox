"""Bluetooth range runs: walks with a player streaming A2DP, logged from this
PC and merged afterwards.

A headset walk keeps the player on the desk on USB, streaming to headphones
a person carries (walk.py logs the player over its dev shell). A player walk
makes this PC the A2DP sink and takes the player away in a pocket (gaps.py
logs the stream's dropouts, bluez.py the link and its RSSI, flow.py reads the
HCI trace). Every tool writes into one run directory under fixed names
(rundir.py), and report.py merges them into one timeline.
"""
