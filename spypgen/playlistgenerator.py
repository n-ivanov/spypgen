import http.server
import itertools
import json
import operator
import os
import pprint 
import requests
import socketserver
import sys
import webbrowser 

import spotipy
import spotipy.util as util

import tqdmredirect
from tracklistscraper import TracklistScraper
from http.server import HTTPServer, BaseHTTPRequestHandler
from collections import namedtuple
from tqdm import tqdm

done = False

class RequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global done
        global auth_code
        auth_code = self.path.replace('/?code=', '')        
        done = True
        self.send_response(200, 'OK')
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write('<html><body><h1>Authentication status: success!</h1>Now you can close this window.</body></html>'.encode('utf-8'))


class Server(socketserver.TCPServer):
    allow_reuse_address = True


SongCounts = namedtuple('SongCounts', ['total', 'include_popular_spotify', 'from_recent_setlists'])
TracklistPreferences = namedtuple('TracklistPreferences', ['num_searched', 'inclusion_threshold'])
class PlaylistGenerator:

    def __init__(self):
        self.username = ''
        self.access_token = ''
        self.spotipy = None
        self.songs_per_artist = SongCounts(total=5,include_popular_spotify=True,from_recent_setlists=0)
        self.tracklist_search_pref = TracklistPreferences(5,0.5)
        self.tracklist_scraper = TracklistScraper()
        self.strict_validation = False
        self.progress_bar = None

    def authorize(self, username, client_id, client_secret, client_port):
        self.username = username
        scope = 'playlist-modify-public'
        authorize_url = 'https://accounts.spotify.com/authorize'
        token_url = 'https://accounts.spotify.com/api/token'
        redirect_uri = f"http://localhost:{str(client_port)}"
        authorization_url = f"{authorize_url}?client_id={client_id}&response_type=code&redirect_uri={redirect_uri}&scope={scope}"
        print(f"Attempting to access {authorization_url}")
        webbrowser.open_new_tab(authorization_url)
        httpd = Server(('localhost', client_port), RequestHandler)
        while not done:
            httpd.handle_request()
        data = {    
            'grant_type':'authorization_code',
            'redirect_uri':redirect_uri,
            'code':auth_code
        }
        access_token_response = requests.post(token_url, data=data,verify=False,allow_redirects=False,auth=(client_id,client_secret))
        tokens = json.loads(access_token_response.text)
        self.access_token = tokens.get('access_token')
        if self.access_token is None:
            return False
        print(f"Access token is: {self.access_token}")
        self.spotipy = spotipy.Spotify(auth=self.access_token)
        self.spotipy.trace = False
        return True

    def set_song_count_preferences(self, total_per_artist, include_popular_spotify, from_recent_setlists):
        total_per_artist = total_per_artist if total_per_artist is not None else self.songs_per_artist.total
        include_popular_spotify = include_popular_spotify if include_popular_spotify is not None else self.songs_per_artist.include_popular_spotify
        from_recent_setlists = from_recent_setlists if from_recent_setlists is not None else self.songs_per_artist.from_recent_setlists
        self.songs_per_artist = SongCounts(total_per_artist, include_popular_spotify, from_recent_setlists)

    def set_tracklist_search_preferences(self, num_searched, inclusion_threshold):
        num_searched = num_searched if num_searched is not None else self.tracklist_search_pref.num_searched
        inclusion_threshold = inclusion_threshold if inclusion_threshold is not None else self.tracklist_search_pref.inclusion_threshold
        self.tracklist_search_pref = TracklistPreferences(num_searched, inclusion_threshold)

    def create_playlist(self,playlist_name,playlist_artists,public=True):
        pbar_len = len(playlist_artists) * (1 + 2 * (1 + self.tracklist_search_pref.num_searched) + self.songs_per_artist.total)
        with tqdmredirect.std_out_err_redirect_tqdm() as orig_stdout:
            with tqdm(total=pbar_len, file=orig_stdout, dynamic_ncols=True) as pbar:
                self.progress_bar = pbar
                #Cannot specify description without JSON errors resulting in Spotipy
                playlistId = self.spotipy.user_playlist_create(self.username, playlist_name, public)['id']
                print(f"Created playlist '{playlist_name}'")
                tracks_by_artists = []
                for artist in playlist_artists:
                    (artist_name, artist_id) = self.find_artist(artist, self.strict_validation)
                    self.update_pbar()
                    tracks_by_artists.extend(self.find_tracks(artist_name,artist_id))
                if len(tracks_by_artists) != 0:
                    uniq_tracks_by_artists = set(tracks_by_artists)
                    self.spotipy.user_playlist_add_tracks(self.username, playlistId, set([track[2] for track in uniq_tracks_by_artists]))
                    print(f"Added {len(uniq_tracks_by_artists)} tracks to playlist '{playlist_name}'")
                    print('Tracklist:')
                    for track in uniq_tracks_by_artists:
                        print(f"{track[1]} - {track[0]}")
                self.complete_progress()

    def find_artist(self,artist_name,strict=False):
        print(f"Searching for artist {artist_name}...")
        foundArtists = self.spotipy.search(artist_name,type='artist')['artists']['items']
        for artist in foundArtists:
            found_artist_name = artist['name']
            if not strict or found_artist_name.lower() == artist_name.lower():
                print(f"Found {found_artist_name}.")
                return (found_artist_name, artist['id'])
            return None

    def find_tracks(self,artist_name,artist_id):
        print(f"Finding top tracks for artist {artist_name}...")
        top_tracks = []
        recent_tracks = []
        if self.songs_per_artist.from_recent_setlists != 0:
            recent_tracks = list(filter(None, [self.find_track(track_name) for track_name in (
                self.tracklist_scraper.get_artists_popular_recent_tracks(
                    artist_name,self.songs_per_artist.from_recent_setlists, 
                    self.tracklist_search_pref.num_searched, 
                    self.tracklist_search_pref.inclusion_threshold,
                    hook=self.update_pbar
                    ))]))
        if self.songs_per_artist.include_popular_spotify and self.songs_per_artist.total > len(recent_tracks):
            num_songs = self.songs_per_artist.total - len(recent_tracks)
            top_tracks = self.spotipy.artist_top_tracks(artist_id)['tracks'][:num_songs]
            self.update_pbar(num_songs)
        return set([(','.join([artist['name'] for artist in track['artists']]),track['name'],track['id']) for track in itertools.chain(top_tracks,recent_tracks)])

    def find_track(self,track_name):
        print(f"Searching for {track_name}...")
        results = self.spotipy.search(track_name.replace('ft.','').replace('&',''),type='track')['tracks']['items']
        self.update_pbar()
        if len(results) == 0:
            return None
        return results[0]

    def update_pbar(self,amount=1):
        if self.progress_bar and self.progress_bar.n + amount <= self.progress_bar.total:
            self.progress_bar.update(amount)

    def complete_progress(self):
        if self.progress_bar:
            self.progress_bar.n = self.progress_bar.total
            self.progress_bar.last_print_n = self.progress_bar.total
            self.progress_bar.refresh()


