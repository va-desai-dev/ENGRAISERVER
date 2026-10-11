// Character Generator Module
class CharacterGenerator {
  constructor() {
    this.apiHandler = null; // Will be set lazily
    this.rawCharacterData = "";
    this.parsedCharacter = null;
  }

  // Lazy getter for apiHandler to avoid circular dependency
  get apiHandlerInstance() {
    if (!this.apiHandler) {
      this.apiHandler = window.apiHandler;
    }
    return this.apiHandler;
  }

  async generateCharacter(concept, characterName, onStream = null, pov = "third", lorebook = null, cardType = "single") {
    try {
      this.rawCharacterData = await this.apiHandlerInstance.generateCharacter(
        concept,
        characterName,
        onStream,
        pov,
        lorebook,
        cardType,
      );
      this.parsedCharacter = this.parseCharacterData(this.rawCharacterData);
      return this.parsedCharacter;
    } catch (error) {
      console.error("Error generating character:", error);
      throw error;
    }
  }

  // Parse character data using simple string splitting based on template
  parseCharacterData(rawData) {
    // Canonical authoring Markdown uses explicit field boundaries. Legacy imports
    // still use the original parser below; never guess sections from prose words.
    if (/^## Background\s*$/m.test(rawData) && /^## Personality\s*$/m.test(rawData)) {
      const sections = {};
      const headings = [...rawData.matchAll(/^## (Background|Personality|Scenario|First Message|Post-History)\s*$/gm)];
      headings.forEach((match, i) => {
        sections[match[1]] = rawData.slice(match.index + match[0].length, headings[i + 1]?.index ?? rawData.length).trim();
      });
      const name = rawData.match(/^# ([^\n]+)$/m)?.[1].trim() || "";
      if (!name || !sections.Background || !sections.Personality) throw new Error("Incomplete character profile; name, background, and personality are required.");
      this.apiHandlerInstance?.recordGeneratedName?.(name);
      return { name, description: sections.Background, personality: sections.Personality,
        scenario: sections.Scenario || "", firstMessage: sections['First Message'] || "",
        postHistoryInstructions: sections['Post-History'] || "", mesExample: "" };
    }

    const character = {
      name: "",
      description: "",
      personality: "",
      scenario: "",
      firstMessage: "",
      mesExample: "",
    };

    // Extract character name from profile section
    // Try standard header format first: # Name's Profile
    const nameMatch = rawData.match(/^#\s*([^'\\]*(?:\\.[^'\\]*)*)'s Profile/i);
    if (nameMatch) {
      character.name = nameMatch[1].trim();
    } else {
      // Try to find name in text (First Person: "The name's Name")
      const nameTextMatch = rawData.match(/The name's\s+([A-Z][a-z]+(?: [A-Z][a-z]+)*)/);
      if (nameTextMatch) {
        character.name = nameTextMatch[1].trim();
      } else {
        // Try Third Person: "Name is..." (at start of description)
        const thirdPersonMatch = rawData.match(/^(?:#\s*[^#\n]+\n+)?([A-Z][a-z]+(?: [A-Z][a-z]+)*)\s+is\b/m);
        if (thirdPersonMatch) {
          character.name = thirdPersonMatch[1].trim();
        }
      }
    }

    // Fallback if name is still missing but we have content
    if (!character.name) {
      console.warn("Could not extract character name. Using default.");
      character.name = "{{char}}";
    }

    // Feed the name into the shared cross-generation history so future
    // auto-generated and picker names avoid repeating it.
    if (this.apiHandlerInstance && typeof this.apiHandlerInstance.recordGeneratedName === "function") {
      this.apiHandlerInstance.recordGeneratedName(character.name);
    }

    // Extract description section (everything from start or # Profile to ## Personality)
    // More robust regex that doesn't strictly require the # Profile header
    const descriptionMatch = rawData.match(
      /(?:#\s*[^#]+?'s Profile[\s\S]*?)?([\s\S]*?)(?=##\s*(?:My\s+)?Personality)/i,
    );
    if (descriptionMatch) {
      // If we captured the header in the group, it's fine. If not, we prepend it if we have a name.
      let descContent = descriptionMatch[1].trim();

      // Clean up potential leading newlines or markdown artifacts
      descContent = descContent.replace(/^#\s*[^#\n]+\n+/, "").trim();

      if (character.name && character.name !== "Unknown Character") {
        character.description = `# ${character.name}'s Profile\n\n${descContent}`;
      } else {
        character.description = descContent;
      }
    }

    // Extract personality section (include the title)
    const personalityMatch = rawData.match(
      /(##\s*(?:My\s+)?Personality[\s\S]*?)(?=#\s*The Roleplay|$)/i,
    );
    if (personalityMatch) {
      character.personality = personalityMatch[1].trim();
    }

    // Extract scenario section (include the title)
    const scenarioMatch = rawData.match(
      /(#\s*The Roleplay's Setup[\s\S]*?)(?=#\s*First Message|$)/i,
    );
    if (scenarioMatch) {
      character.scenario = scenarioMatch[1].trim();
    } else {
      // Create a default scenario if not found
      character.scenario = `A roleplay featuring ${character.name}. The setting and circumstances evolve naturally through interaction between ${character.name} and {{user}}.`;
    }

    // Extract first message (no title, just the content)
    const firstMessageMatch = rawData.match(
      /#\s*First Message\s*\n\n([\s\S]+?)$/i,
    );
    if (firstMessageMatch) {
      character.firstMessage = firstMessageMatch[1].trim();
    } else {
      // Try with single newline
      const firstMessageMatchAlt = rawData.match(
        /#\s*First Message\s*\n([\s\S]+?)$/i,
      );
      if (firstMessageMatchAlt) {
        character.firstMessage = firstMessageMatchAlt[1].trim();
      }
    }

    // Safety check: forcibly replace the character's name with {{char}} in most fields
    // to guarantee engine compatibility if the AI forgot the rule.
    if (character.name && character.name !== "{{char}}" && character.name.toLowerCase() !== "unknown character") {
      try {
        const escapedFullName = character.name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const fullRegex = new RegExp(`\\b${escapedFullName}\\b`, 'g');
        
        let firstRegex = null;
        const nameParts = character.name.split(" ");
        if (nameParts.length > 1 && nameParts[0].length > 2) {
          const escapedFirstName = nameParts[0].replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
          firstRegex = new RegExp(`\\b${escapedFirstName}\\b`, 'g');
        }

        const enforceMacro = (text) => {
          if (!text) return text;
          let result = text.replace(fullRegex, '{{char}}');
          if (firstRegex) result = result.replace(firstRegex, '{{char}}');
          return result;
        };

        if (character.description) {
          const lines = character.description.split('\n');
          let safeStartIndex = 0;
          let foundIntro = false;
          
          for (let i = 0; i < lines.length; i++) {
            if (lines[i].trim() !== '' && !lines[i].startsWith('#') && !lines[i].startsWith('**') && !foundIntro) {
              foundIntro = true;
              continue; // Skip the intro paragraph where name is allowed
            }
            if (foundIntro) {
              safeStartIndex = i;
              break;
            }
          }
          
          for (let i = safeStartIndex; i < lines.length; i++) {
            lines[i] = enforceMacro(lines[i]);
          }
          character.description = lines.join('\n');
        }

        character.personality = enforceMacro(character.personality);
        character.scenario = enforceMacro(character.scenario);
        character.firstMessage = enforceMacro(character.firstMessage);
        if (character.mesExample) character.mesExample = enforceMacro(character.mesExample);
      } catch (e) {
        console.warn("Failed to apply {{char}} macro enforcement:", e);
      }
    }

    return character;
  }


  toSpecV2Format(character) { return window.CardV2.toV2(character); }


}

// Export singleton instance
window.characterGenerator = new CharacterGenerator();
