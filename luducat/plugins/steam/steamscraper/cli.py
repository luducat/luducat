#!/usr/bin/env python3
# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# cli.py

"""
CLI application for testing Steam Scraper module.
"""

import argparse
import logging
import sys
from luducat.plugins.sdk.json import json
from steam_scraper import SteamGameManager
from steam_scraper.exceptions import SteamScraperException

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def print_game_info(game):
    """Print game information in a readable format."""
    print("\n" + "="*60)
    print(f"App ID: {game.appid}")
    print(f"Name: {game.name}")
    print(f"Developers: {', '.join(game.developers) if game.developers else 'N/A'}")
    print(f"Publishers: {', '.join(game.publishers) if game.publishers else 'N/A'}")
    print(f"Release Date: {game.release_date or 'N/A'}")
    print(f"Price: ${game.price:.2f}" if game.price else "Price: Free/N/A")
    print("Platforms: ", end="")
    platforms = []
    if game.windows:
        platforms.append("Windows")
    if game.mac:
        platforms.append("Mac")
    if game.linux:
        platforms.append("Linux")
    print(", ".join(platforms) if platforms else "N/A")
    
    if game.genres:
        print(f"Genres: {', '.join(game.genres)}")
    
    if game.short_description:
        print(f"\nDescription: {game.short_description[:200]}...")
    
    if game.metacritic_score:
        print(f"Metacritic Score: {game.metacritic_score}")
    
    print(f"Complete: {'Yes' if game.is_complete else 'No'}")
    print(f"Last Updated: {game.last_updated}")
    
    if game.images:
        print(f"\nScreenshots: {len(game.images)} images")
        for img in game.images[:3]:
            print(f"  - {img.filename}")
        if len(game.images) > 3:
            print(f"  ... and {len(game.images) - 3} more")
    
    print("="*60 + "\n")


def cmd_get_game(args):
    """Get game by appid or name."""
    manager = SteamGameManager()
    
    try:
        if args.appid:
            logger.info(f"Fetching game with appid: {args.appid}")
            game = manager.get_game(appid=args.appid)
        elif args.name:
            logger.info(f"Searching for game: {args.name}")
            game = manager.get_game(name=args.name)
        else:
            logger.error("Must provide either --appid or --name")
            return 1
        
        print_game_info(game)
        
        if args.json:
            # Output as JSON
            output = {
                'appid': game.appid,
                'name': game.name,
                'developers': game.developers,
                'publishers': game.publishers,
                'release_date': game.release_date,
                'price': game.price,
                'platforms': {
                    'windows': game.windows,
                    'mac': game.mac,
                    'linux': game.linux
                },
                'genres': game.genres,
                'categories': game.categories,
                'is_complete': game.is_complete
            }
            print(json.dumps(output, indent=2))
        
        return 0
        
    except SteamScraperException as e:
        logger.error(f"Error: {e}")
        return 1
    finally:
        manager.close()


def cmd_import_userdata(args):
    """Import owned apps from Steam dynamicstore/userdata JSON."""
    manager = SteamGameManager()
    
    try:
        logger.info(f"Importing Steam userdata from: {args.json_file}")
        
        print("\nInstructions:")
        print("  1. Log into Steam in your browser")
        print("  2. Visit: https://store.steampowered.com/dynamicstore/userdata/")
        print("  3. Save the JSON response to a file")
        print("  4. Run: ./cli.py import-userdata <file.json>")
        print()
        
        stats = manager.import_steam_userdata(
            args.json_file,
            fetch_missing=not args.no_fetch
        )
        
        print("\nImport Statistics:")
        print(f"  Total owned apps: {stats['total_owned']}")
        print(f"  Already in database: {stats['already_in_db']}")
        print(f"  Newly fetched: {stats['fetched']}")
        print(f"  Failed to fetch: {stats['failed']}")
        print(f"  Wishlist count: {stats['wishlist_count']}")
        print(f"  Ignored apps: {stats['ignored_count']}")
        
        if stats['failed'] > 0:
            print(f"\n⚠ {stats['failed']} apps failed to fetch - check logs for details")
        
        if stats['fetched'] > 0:
            print(f"\n✓ Successfully populated cache with {stats['fetched']} new games!")
        elif stats['total_owned'] > 0:
            print(f"\n✓ All {stats['total_owned']} owned apps already in database!")
        
        return 0
        
    except SteamScraperException as e:
        logger.error(f"Error: {e}")
        return 1
    except FileNotFoundError:
        logger.error(f"File not found: {args.json_file}")
        print("\nTo get your userdata JSON:")
        print("  1. Log into Steam in your browser")
        print("  2. Visit: https://store.steampowered.com/dynamicstore/userdata/")
        print("  3. Save the page as a .json file")
        return 1
    finally:
        manager.close()

def cmd_search(args):
    """Search for games by name."""
    manager = SteamGameManager()
    
    try:
        logger.info(f"Searching for: {args.query}")
        
        # Search in database
        from steam_scraper.database import Game
        session = manager.database.get_session()
        
        games = session.query(Game).filter(
            Game.name.ilike(f"%{args.query}%")
        ).limit(args.limit).all()
        
        if not games:
            print(f"No games found matching '{args.query}'")
            return 0
        
        print(f"\nFound {len(games)} game(s):")
        for game in games:
            print(f"  {game.appid}: {game.name}")
            if game.developers:
                print(f"    Developers: {', '.join(game.developers)}")
        
        session.close()
        return 0
        
    except Exception as e:
        logger.error(f"Error: {e}")
        return 1
    finally:
        manager.close()


def cmd_refresh_images(args):
    """Refresh image cache for games."""
    manager = SteamGameManager()
    
    try:
        if args.appid:
            # Refresh specific game
            logger.info(f"Refreshing images for appid: {args.appid}")
            stats = manager.refresh_images(args.appid)
            
            print(f"\nImage Refresh Results for App {args.appid}:")
            print(f"  Downloaded: {stats['downloaded']}")
            print(f"  Updated: {stats['updated']}")
            print(f"  Skipped (up-to-date): {stats['skipped']}")
            
        elif args.all:
            # Refresh all games
            logger.info("Refreshing images for all games in database...")
            print("\nRefreshing images for all games...")
            print("This may take a while depending on database size.\n")
            
            stats = manager.refresh_all_images(limit=args.limit)
            
            print("\nOverall Image Refresh Results:")
            print(f"  Games processed: {stats['games_processed']}")
            print(f"  Games failed: {stats['games_failed']}")
            print(f"  Total downloaded: {stats['total_downloaded']}")
            print(f"  Total updated: {stats['total_updated']}")
            print(f"  Total skipped: {stats['total_skipped']}")
            
        else:
            logger.error("Must specify either --appid or --all")
            return 1
        
        return 0
        
    except SteamScraperException as e:
        logger.error(f"Error: {e}")
        return 1
    finally:
        manager.close()


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description='Steam Scraper CLI - Manage Steam game data',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    subparsers = parser.add_subparsers(dest='command', help='Command to execute')
    
    # Get game command
    get_parser = subparsers.add_parser('get', help='Get game by appid or name')
    get_parser.add_argument('--appid', type=int, help='Steam App ID')
    get_parser.add_argument('--name', type=str, help='Game name')
    get_parser.add_argument('--json', action='store_true', help='Output as JSON')
    get_parser.set_defaults(func=cmd_get_game)

    # Import Steam userdata command
    userdata_parser = subparsers.add_parser(
        'import-userdata', 
        help='Import owned apps from Steam dynamicstore/userdata JSON'
    )
    userdata_parser.add_argument('json_file', help='Path to userdata JSON file')
    userdata_parser.add_argument(
        '--no-fetch', 
        action='store_true', 
        help='Only check database, do not fetch missing games from Steam'
    )
    userdata_parser.set_defaults(func=cmd_import_userdata)
    
    # Search command
    search_parser = subparsers.add_parser('search', help='Search for games in database')
    search_parser.add_argument('query', help='Search query')
    search_parser.add_argument('--limit', type=int, default=10, help='Max results (default: 10)')
    search_parser.set_defaults(func=cmd_search)
    
    # Refresh images command
    refresh_parser = subparsers.add_parser('refresh', help='Refresh image cache')
    refresh_parser.add_argument('--appid', type=int, help='Refresh images for specific app ID')
    refresh_parser.add_argument('--all', action='store_true', help='Refresh images for all games')
    refresh_parser.add_argument('--limit', type=int, help='Limit number of games when using --all')
    refresh_parser.set_defaults(func=cmd_refresh_images)
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return 1
    
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
