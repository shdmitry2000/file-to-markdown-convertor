#!/usr/bin/env python3
"""Test markdown cleaning function with Hebrew text.

This test verifies that the _clean_markdown_content function:
1. Preserves Hebrew and other Unicode text
2. Removes null bytes that PostgreSQL can't handle
3. Removes control characters that cause parsing issues
4. Maintains proper UTF-8 encoding
"""

import sys
from pathlib import Path

# Add app to path
sys.path.insert(0, str(Path(__file__).parent / "app" / "workers"))

from worker import _clean_markdown_content


def test_hebrew_preservation():
    """Test that Hebrew text is preserved."""
    test_cases = [
        {
            "name": "Simple Hebrew text",
            "input": "תקנה 439 עוסקת בנושא חשוב",
            "should_contain": ["תקנה", "439", "עוסקת", "בנושא", "חשוב"],
            "description": "Basic Hebrew words with numbers"
        },
        {
            "name": "Hebrew with punctuation",
            "input": "תקנה 439: הוראות חשובות.",
            "should_contain": ["תקנה", "439", "הוראות", "חשובות", ":", "."],
            "description": "Hebrew with colon and period"
        },
        {
            "name": "Mixed Hebrew and English",
            "input": "Section 439: תקנה חשובה very important",
            "should_contain": ["Section", "439", "תקנה", "חשובה", "important"],
            "description": "Bilingual content"
        },
        {
            "name": "Hebrew in markdown headers",
            "input": "## תקנה 439\n\nהסבר על התקנה",
            "should_contain": ["##", "תקנה", "439", "הסבר", "על", "התקנה"],
            "description": "Hebrew in markdown syntax"
        },
        {
            "name": "Real Hebrew document fragment",
            "input": ".6 .   ,    (תקנות לפיקדון)  - ,       (תקנות למשכנתא)   :\n)12/21( ]9\n)1(       ,        תנאים,        ,   ,   ' .",
            "should_contain": ["תקנות", "לפיקדון", "למשכנתא", "תנאים"],
            "description": "Fragment from actual Hebrew banking document"
        }
    ]
    
    print("\n" + "=" * 70)
    print("TEST: Hebrew Text Preservation")
    print("=" * 70)
    
    all_passed = True
    
    for test in test_cases:
        print(f"\n📝 {test['name']}")
        print(f"   Description: {test['description']}")
        print(f"   Input: {test['input'][:60]}...")
        
        result = _clean_markdown_content(test['input'])
        print(f"   Output: {result[:60]}...")
        
        test_passed = True
        for expected in test['should_contain']:
            if expected in result:
                print(f"   ✅ Contains '{expected}'")
            else:
                print(f"   ❌ MISSING '{expected}'")
                test_passed = False
                all_passed = False
        
        if test_passed:
            print(f"   ✅ Test PASSED")
        else:
            print(f"   ❌ Test FAILED")
    
    return all_passed


def test_problematic_characters_removal():
    """Test that problematic characters are removed."""
    test_cases = [
        {
            "name": "Null bytes removed",
            "input": "Hello\x00World\x00Test",
            "should_not_contain": ["\x00"],
            "should_contain": ["Hello", "World", "Test"],
            "description": "PostgreSQL cannot store null bytes"
        },
        {
            "name": "Control characters removed",
            "input": "Hello\x01\x02World\x1F\x7FTest",
            "should_not_contain": ["\x01", "\x02", "\x1F", "\x7F"],
            "should_contain": ["Hello", "World", "Test"],
            "description": "ASCII control chars cause parsing issues"
        },
        {
            "name": "Tabs and newlines preserved",
            "input": "Line1\nLine2\tTabbed",
            "should_contain": ["\n", "\t", "Line1", "Line2", "Tabbed"],
            "description": "Valid whitespace should remain"
        },
        {
            "name": "Mixed problematic and valid",
            "input": "תקנה\x00439\nסעיף\x01חשוב",
            "should_not_contain": ["\x00", "\x01"],
            "should_contain": ["תקנה", "439", "\n", "סעיף", "חשוב"],
            "description": "Remove control chars but keep Hebrew and valid whitespace"
        }
    ]
    
    print("\n" + "=" * 70)
    print("TEST: Problematic Characters Removal")
    print("=" * 70)
    
    all_passed = True
    
    for test in test_cases:
        print(f"\n📝 {test['name']}")
        print(f"   Description: {test['description']}")
        print(f"   Input: {repr(test['input'][:60])}")
        
        result = _clean_markdown_content(test['input'])
        print(f"   Output: {repr(result[:60])}")
        
        test_passed = True
        
        # Check should_contain
        if 'should_contain' in test:
            for expected in test['should_contain']:
                if expected in result:
                    print(f"   ✅ Contains {repr(expected)}")
                else:
                    print(f"   ❌ MISSING {repr(expected)}")
                    test_passed = False
                    all_passed = False
        
        # Check should_not_contain
        if 'should_not_contain' in test:
            for unexpected in test['should_not_contain']:
                if unexpected not in result:
                    print(f"   ✅ Removed {repr(unexpected)}")
                else:
                    print(f"   ❌ Still contains {repr(unexpected)}")
                    test_passed = False
                    all_passed = False
        
        if test_passed:
            print(f"   ✅ Test PASSED")
        else:
            print(f"   ❌ Test FAILED")
    
    return all_passed


def test_encoding_safety():
    """Test UTF-8 encoding safety."""
    test_cases = [
        {
            "name": "Hebrew UTF-8",
            "input": "תקנה 439",
            "description": "Hebrew characters are valid UTF-8"
        },
        {
            "name": "Arabic UTF-8",
            "input": "مادة ٤٣٩",
            "should_contain": ["مادة", "٤٣٩"],
            "description": "Arabic characters are valid UTF-8"
        },
        {
            "name": "Chinese UTF-8",
            "input": "规则 439",
            "should_contain": ["规则", "439"],
            "description": "Chinese characters are valid UTF-8"
        },
        {
            "name": "Emoji UTF-8",
            "input": "Document 📄 section ✅",
            "should_contain": ["Document", "section"],
            "description": "Emojis are valid UTF-8"
        }
    ]
    
    print("\n" + "=" * 70)
    print("TEST: UTF-8 Encoding Safety")
    print("=" * 70)
    
    all_passed = True
    
    for test in test_cases:
        print(f"\n📝 {test['name']}")
        print(f"   Description: {test['description']}")
        print(f"   Input: {test['input']}")
        
        try:
            result = _clean_markdown_content(test['input'])
            print(f"   Output: {result}")
            
            # Verify it can be encoded as UTF-8
            encoded = result.encode('utf-8')
            decoded = encoded.decode('utf-8')
            
            if decoded == result:
                print(f"   ✅ UTF-8 encoding round-trip successful")
            else:
                print(f"   ❌ UTF-8 encoding round-trip FAILED")
                all_passed = False
            
            # Check should_contain if specified
            if 'should_contain' in test:
                test_passed = True
                for expected in test['should_contain']:
                    if expected in result:
                        print(f"   ✅ Contains '{expected}'")
                    else:
                        print(f"   ❌ MISSING '{expected}'")
                        test_passed = False
                        all_passed = False
                
                if test_passed:
                    print(f"   ✅ Test PASSED")
                else:
                    print(f"   ❌ Test FAILED")
            else:
                print(f"   ✅ Test PASSED")
                
        except Exception as e:
            print(f"   ❌ Exception: {e}")
            all_passed = False
    
    return all_passed


def main():
    """Run all tests."""
    print("\n")
    print("╔" + "=" * 68 + "╗")
    print("║" + " " * 15 + "MARKDOWN CLEANING FUNCTION TESTS" + " " * 20 + "║")
    print("╚" + "=" * 68 + "╝")
    
    results = []
    
    # Run all test suites
    results.append(("Hebrew Preservation", test_hebrew_preservation()))
    results.append(("Problematic Chars Removal", test_problematic_characters_removal()))
    results.append(("UTF-8 Encoding Safety", test_encoding_safety()))
    
    # Summary
    print("\n" + "=" * 70)
    print("TEST SUMMARY")
    print("=" * 70)
    
    total_passed = sum(1 for _, passed in results if passed)
    total_tests = len(results)
    
    for suite_name, passed in results:
        status = "✅ PASSED" if passed else "❌ FAILED"
        print(f"{suite_name:.<50} {status}")
    
    print("=" * 70)
    print(f"Total: {total_passed}/{total_tests} test suites passed")
    print("=" * 70)
    
    if total_passed == total_tests:
        print("\n🎉 All tests PASSED! The cleaning function works correctly.")
        return 0
    else:
        print(f"\n⚠️  {total_tests - total_passed} test suite(s) FAILED. Hebrew text is being stripped!")
        print("\nExpected behavior:")
        print("  - Hebrew/Arabic/Chinese text should be PRESERVED")
        print("  - Only null bytes and control chars should be REMOVED")
        print("  - Current implementation likely uses ASCII-only filtering")
        return 1


if __name__ == "__main__":
    exit(main())
