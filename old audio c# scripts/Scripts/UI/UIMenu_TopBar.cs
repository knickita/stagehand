using System.Collections;
using System.Collections.Generic;
using UnityEngine;
using StringMath;
using TMPro;
using UnityEngine.UI;

public class UIMenu_TopBar : MonoBehaviour
{
    public TMP_InputField frequencyInput, maxDbInput;
    public Slider frequencySlider;
    public Toggle displayGradient;
    AudioManager audioManager;
    float oldFrequency, oldMaxDb;
    // Start is called before the first frame update
    void Start()
    {
        audioManager=FindObjectOfType<AudioManager>();
        oldFrequency=audioManager.frequency;
        oldMaxDb=audioManager.maxDisplayedSpl;
        frequencyInput.text=oldFrequency.ToString();
        maxDbInput.text=oldMaxDb.ToString();
        displayGradient.isOn = audioManager.displayGradient;
    }

    // Update is called once per frame
    void Update()
    {
        
    }

    public string ValidateValue(string value)
    {
        value=value.Replace(',','.');
        try{     
            MathExpr expr = value;
            float f = (float) expr.Result;
            if (f<=0){
                return null;
            }
            return f.ToString().Replace(',','.');            
        }
        catch(MathException exception){
            Debug.Log(exception.Message);
            return null;
        }
    }

    public void ChangeFrequency(string s){
        string validated = ValidateValue(s);
        if (validated==null){
            frequencyInput.text=oldFrequency.ToString();
            return;
        }
        float f = float.Parse(validated);
        audioManager.frequency=f;
        oldFrequency=f;
        frequencyInput.text=validated;
        frequencySlider.value=Mathf.Log10(f/20.0f)/3.0f;
        
    }
    public void ChangeFrequencySlider(float f){
        //map f from 0-1 to 20-20000 logaritmic
        f=Mathf.Floor(Mathf.Pow(10,f*3.0f+1.35f));
        audioManager.frequency=f;
        oldFrequency=f;
        frequencyInput.text=f.ToString();
    }

    public void ChangeMaxDb(string s){
        string validated = ValidateValue(s);
        if (validated==null){
            maxDbInput.text=audioManager.maxDisplayedSpl.ToString();
            return;
        }
        float f = float.Parse(validated);
        audioManager.maxDisplayedSpl=f;
        maxDbInput.text=validated;
    }

    public void ChangeDisplayGradient(bool b){
        audioManager.displayGradient=b;
    }
}
