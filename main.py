import speech_recognition as sr
import pyttsx3  
import pygame
import os
from gtts import gTTS
import musicLibrary 
import requests
import webbrowser
from openai import OpenAI

recognizer = sr.Recognizer()
engine = pyttsx3.init()
pygame.mixer.init()
newsapi = "732672f8b79047cfb5868dcad7dfd835"
def speak_old(text):
    engine.say(text)
    engine.runAndWait()


def speak(text):
    tts = gTTS(text)
    tts.save('temp.mp3') 
    pygame.mixer.music.load('temp.mp3')
    pygame.mixer.music.play()
    while pygame.mixer.music.get_busy():
        pygame.time.Clock().tick(10)
    pygame.mixer.music.unload()
    os.remove("temp.mp3") 

def processCommand(c):
    if "open google" in c.lower():
        webbrowser.open("https://google.com")
        # return "Opening Google...."
    elif "open facebook" in c.lower():
        webbrowser.open("https://facebook.com")
        # return "Opening Facebook"
    elif "open youtube" in c.lower():
        webbrowser.open("https://youtube.com")
        # return "Opening Youtube"
    elif "open linkedin" in c.lower():
        webbrowser.open("https://linkedin.com")
        # return "Opening Linkedin"
    elif "open instagram" in c.lower():
        webbrowser.open("www.instagram.com")
    elif "open w3school" in c.lower():
        webbrowser.open("www.w3school.com")
    elif "open vulms" in c.lower():
        webbrowser.open("https://vulms.vu.edu.pk")
        # return "Opening VULMS"
    elif "open digiskill" in c.lower():
        webbrowser.open("https://lms.digiskills.pk/Login.aspx")
    elif "open hunarmandpunjab" in c.lower():
        webbrowser.open("https://hunarmandpunjab.pk/login")
        # return "Opening HunarmandPunjab"
    elif c.lower().startswith("play"):
        song = c.lower().split(" ")[1]
        link = musicLibrary.music[song]
        webbrowser.open(link)
    elif "news" in c.lower():
        r = requests.get(f"https://newsapi.org/v2/top-headlines?country=in&apiKey={newsapi}")
        if r.status_code == 200:
            data = r.json()
            articles = data.get("articles", [])
            for article in articles[:5]:
                speak(article["title"])
    
    else:
        output = aiProcess(c)
        speak(output)
    


def aiProcess(command):
    client = OpenAI(api_key="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")
    completion = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "You are a virtual assistant named jarvis..."},
            {"role": "user", "content": command}
        ]
    )
    return completion.choices[0].message.content

if __name__ == "__main__":
    speak("Initializing Jarvis....")
    while True:
        r = sr.Recognizer()
        print("recognizing...")
        try:
            with sr.Microphone() as source:
                print("Listening...")
                audio = r.listen(source, timeout=2, phrase_time_limit=1)
            word = r.recognize_google(audio)
            if(word.lower() == "jarvis"):
                speak("Hi this is jarvis how can i help you?")
                with sr.Microphone() as source:
                    print("Jarvis Active...")
                    audio = r.listen(source)
                    command = r.recognize_google(audio)
                    processCommand(command)

        except Exception as e:
            print("Error; {0}".format(e))
